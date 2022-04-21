import os
os.environ['CUDA_VISIBLE_DEVICES'] = "1, 2, 3, 4, 5, 6, 7"
import argparse
import logging
import random
from collections import OrderedDict, defaultdict
import numpy as np
import torch
import pandas as pd
import torch.utils.data
from tqdm import trange
from experiments.adult.models import NN_Context, NNHyper, LRHyper, LR_Context
from experiments.adult.node import BaseNodes
from experiments.adult.utils import get_device, seed_everything, set_logger, TP_FP_TN_FN, metrics
from torch.utils.tensorboard import SummaryWriter
import matplotlib.pyplot as plt
import seaborn as sn
from fairtorch import DemographicParityLoss, EqualiedOddsLoss
from ray import tune
from ray.tune.schedulers import ASHAScheduler
from ray.tune import CLIReporter
from functools import partial

def eval_model(nodes, num_nodes, hnet, model, loss, device, fair, fair_loss,confusion=False):
    curr_results, pred, true, f1, f1_f, f1_m, a, f_a, m_a, aod, eod, spd = evaluate(nodes, num_nodes, hnet, model, loss, device, fair, fair_loss)
    total_correct = sum([val['correct'] for val in curr_results.values()])
    total_samples = sum([val['total'] for val in curr_results.values()])
    avg_loss = np.mean([val['loss'] for val in curr_results.values()])
    avg_acc = total_correct / total_samples

    all_acc = [val['correct'] / val['total'] for val in curr_results.values()]
    all_loss = [val['loss'] for val in curr_results.values()]

    if confusion:

        for i in range(len(pred)):
            actual = pd.Series(true[i], name='Actual')
            prediction = pd.Series(pred[i], name='Predicted')
            confusion = pd.crosstab(actual, prediction)
            print(confusion)
            plt.figure(figsize=(12, 7))
            sn.heatmap(confusion, annot=True)
            title = 'Confusion Matrix for Client ' + str(i + 1)
            plt.title(title)
            plt.show()

    return curr_results, avg_loss, avg_acc, all_acc, all_loss, f1, f1_f, f1_m, f_a, m_a, aod, eod, spd

@torch.no_grad()
def evaluate(nodes, num_nodes, hnet, model, loss, device, fair, fair_loss):
    hnet.eval()
    results = defaultdict(lambda: defaultdict(list))
    preds = []
    true = []
    f1, f1_f, f1_m, a, f_a, m_a, aod, eod, spd = [], [], [], [], [], [], [], [], []

    for node_id in range(num_nodes):
        pred_client = []
        true_client = []
        queries_client = []
        sensitive_client = []
        running_loss, running_correct, running_samples = 0, 0, 0

        curr_data = nodes.test_loaders[node_id]


        for batch_count, batch in enumerate(curr_data):
            x, y, s = tuple((t.type(torch.cuda.FloatTensor)).to(device) for t in batch)
            true_client.extend(y.cpu().numpy())
            queries_client.extend(x.cpu().numpy())
            sensitive_client.extend(s.cpu().numpy())

            avg_context_vector = model(x.to(device), context_only=True)
            weights = hnet(avg_context_vector, torch.tensor([node_id], dtype=torch.long).to(device))
            net_dict = model.state_dict()
            hnet_dict = {k: v for k, v in weights.items() if k in net_dict}
            net_dict.update(hnet_dict)
            model.load_state_dict(net_dict)

            pred = model(x, context_only = False)
            pred_prob = torch.sigmoid(pred)
            pred_thresh = (pred_prob > 0.5).long()
            pred_client.extend(pred_thresh.flatten().cpu().numpy())

            if fair == 'none':
                running_loss += loss(pred, y.unsqueeze(1)).item()
            else:
                running_loss += (loss(pred, y.unsqueeze(1)) + fair_loss(x, pred, s, y).to(device)).item()

            correct = torch.eq(pred_thresh,y.unsqueeze(1)).type(torch.cuda.LongTensor)
            running_correct += torch.count_nonzero(correct).item()

            running_samples += len(y)

        if fair == 'none':
            tp, fp, tn, fn = TP_FP_TN_FN(queries_client, pred_client, true_client, fair)
        else:
            tp, fp, tn, fn = TP_FP_TN_FN(sensitive_client, pred_client, true_client, fair)

        f1_score_prediction, f1_female, f1_male, accuracy, f_acc, m_acc, AOD, EOD, SPD = metrics(tp, fp, tn, fn)
        f1.append(f1_score_prediction)
        f1_f.append(f1_female)
        f1_m.append(f1_male)
        a.append(accuracy)
        f_a.append(f_acc)
        m_a.append(m_acc)
        aod.append(AOD)
        eod.append(EOD)
        spd.append(SPD)
        results[node_id]['loss'] = running_loss / (batch_count + 1)
        results[node_id]['correct'] = running_correct
        results[node_id]['total'] = running_samples
        preds.append(pred_client)
        true.append(true_client)

    return results, preds, true, f1, f1_f, f1_m, a, f_a, m_a, aod, eod, spd

#def train(config):
def train(writer, data_name,model_name,classes_per_node,num_nodes,steps,inner_steps,lr,inner_lr,wd,inner_wd, hyper_hid,n_hidden,bs,device,eval_every,alpha,fair):

    seed_everything(0)
    # data_name="compas"
    # model_name="LR"
    # classes_per_node = 2
    # num_nodes=4
    # steps=1000
    # inner_steps=50
    # lr = config['h-lr']
    # inner_lr = config['inner-lr']
    # wd = config['wd']
    # inner_wd = config['inner-wd']
    # hyper_hid = 100
    # n_hidden = 3
    # bs = 256
    # alpha = config['alpha']
    # fair = "dp"

    nodes = BaseNodes(data_name, num_nodes, bs, classes_per_node, fair)

    num_features = len(nodes.features)

    embed_dim = num_features

    if model_name == 'NN':
        hnet = NNHyper(n_nodes=num_nodes, embedding_dim=embed_dim, context_vector_size=num_features, hidden_size=num_features, hnet_hidden_dim = hyper_hid, hnet_n_hidden=n_hidden)
        model = NN_Context(input_size=num_features, context_vector_size=num_features, context_hidden_size=50, nn_hidden_size=num_features, dropout=.5)
    if model_name == 'LR':
        hnet = LRHyper(n_nodes=num_nodes, embedding_dim=embed_dim, context_vector_size=num_features, hidden_size=num_features, hnet_hidden_dim=hyper_hid, hnet_n_hidden=n_hidden)
        model = LR_Context(input_size=num_features, context_vector_size=num_features, context_hidden_size=50, nn_hidden_size=num_features)


    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda:4"

    hnet.to(device)
    model.to(device)

    optimizer = torch.optim.Adam(params=hnet.parameters(), lr=lr, weight_decay=wd)
    loss = torch.nn.BCEWithLogitsLoss()

    if fair == 'dp':
        fair_loss = DemographicParityLoss(sensitive_classes=[0, 1], alpha=alpha)
    if fair == 'eo':
        fair_loss = EqualiedOddsLoss(sensitive_classes=[0, 1], alpha=alpha)

    step_iter = trange(steps)
    #step_iter = range(steps)

    for step in step_iter:
        # sample client
        hnet.train()
        node_id = random.choice(range(num_nodes))

        if fair == "both":
            if node_id % 2 == 0:
                fair_loss = DemographicParityLoss(sensitive_classes=[0, 1], alpha=50)
            else:
                fair_loss = EqualiedOddsLoss(sensitive_classes=[0, 1], alpha=75)

        for j in range(inner_steps):
            # get new batch
            batch = next(iter(nodes.train_loaders[node_id]))
            x, y, s = tuple((t.type(torch.cuda.FloatTensor)).to(device) for t in batch)

            # if torch.min(x, dim=0).values[0] < 31 and torch.max(x, dim=0).values[0] > 31:
            #     print('True:', torch.min(x, dim=0).values[0], torch.max(x, dim=0).values[0])

           # get parameters based on c^i if it is the first local epoch
            if j == 0:
                avg_context_vector = model(x, context_only=True)
                weights = hnet(avg_context_vector, torch.tensor([node_id], dtype=torch.long).to(device))
                net_dict = model.state_dict()
                hnet_dict = {k: v for k, v in weights.items() if k in net_dict}
                net_dict.update(hnet_dict)
                model.load_state_dict(net_dict)

                inner_optim = torch.optim.Adam(model.parameters(), lr=inner_lr, weight_decay=inner_wd)

                # save starting config
                inner_state = OrderedDict({k: tensor.data for k, tensor in weights.items()})

            model.train()
            inner_optim.zero_grad()
            optimizer.zero_grad()

            # train and update local
            pred = model(x, context_only=False)
            if fair == 'none':
                err = loss(pred, y.unsqueeze(1))
            else:
                err = loss(pred, y.unsqueeze(1)) + fair_loss(x, pred, s, y).to(device)

            err.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 50)
            inner_optim.step()

        # delta theta and global updates
        optimizer.zero_grad()
        final_state = model.state_dict()
        delta_theta = OrderedDict({k: inner_state[k] - final_state[k] for k in weights.keys()})
        hnet_grads = torch.autograd.grad(list(weights.values()), hnet.parameters(), grad_outputs=list(delta_theta.values()))

        for p, g in zip(hnet.parameters(), hnet_grads):
            p.grad = g

        torch.nn.utils.clip_grad_norm_(hnet.parameters(), 50)
        optimizer.step()

        if step % eval_every == 49 or step == 999 or step == 0:
            step_results, avg_loss, avg_acc, all_acc, all_loss, f1, f1_f, f1_m, f_a, m_a, aod, eod, spd = eval_model(nodes, num_nodes, hnet, model, loss, device, confusion=False, fair=fair, fair_loss=fair_loss)

            logging.info(f"\nStep: {step + 1}, AVG Loss: {avg_loss:.4f},  AVG Acc: {avg_acc:.4f}")
            writer.add_scalars('testing accuracy', {
                'average': avg_acc,
                'client 1': all_acc[0],
                'client 2': all_acc[1],
                'client 3': all_acc[2],
                'client 4': all_acc[3]
            }, step)
            writer.add_scalars('testing loss', {
                'average': avg_loss,
                'client 1': all_loss[0],
                'client 2': all_loss[1],
                'client 3': all_loss[2],
                'client 4': all_loss[3]
            }, step)


    step_results, avg_loss, avg_acc, all_acc, all_loss, f1, f1_f, f1_m, f_a, m_a, aod, eod, spd = eval_model(nodes, num_nodes, hnet, model, loss, device, confusion=False,fair=fair, fair_loss = fair_loss)
    logging.info(f"\n\nFinal Results | AVG Loss: {avg_loss:.4f},  AVG Acc: {avg_acc:.4f}")
    for i in range(num_nodes):
        print("\nClient", i+1)
        print(f"Acc: {all_acc[i]:.4f}, F Acc: {f_a[i]:.4f}, M Acc: {m_a[i]:.4f}, F1: {f1[i]:.4f}, AOD: {aod[i]:.4f}, EOD: {eod[i]:.4f}, SPD: {spd[i]:.4f}")
    # tune.report(accuracy=avg_acc, SPD=abs(np.max(spd)))

def main(num_samples, max_num_epochs, gpus_per_trial):
    pd.set_option('display.float_format', lambda x: '%.1f' % x)

    writer = SummaryWriter('results')

    parser = argparse.ArgumentParser(description="Fair Hypernetworks")

    parser.add_argument("--data_name", type=str, default="compas", choices=["adult", "compas"], help="choice of dataset")
    parser.add_argument("--model_name", type=str, default="NN", choices=["NN", "LR"], help="choice of model")
    parser.add_argument("--num_nodes", type=int, default=4, help="number of simulated clients")
    parser.add_argument("--num_steps", type=int, default=1000)
    parser.add_argument("--batch_size", type=int, default=256)
    parser.add_argument("--inner_steps", type=int, default=50, help="number of inner steps")
    parser.add_argument("--n_hidden", type=int, default=3, help="num. hidden layers")
    parser.add_argument("--inner_lr", type=float, default=1e-3, help="learning rate for inner optimizer")
    parser.add_argument("--lr", type=float, default=1e-3, help="learning rate")
    parser.add_argument("--wd", type=float, default=1e-5, help="weight decay")
    parser.add_argument("--inner_wd", type=float, default=1e-5, help="inner weight decay")
    parser.add_argument("--embed_dim", type=int, default=10, help="embedding dim")
    parser.add_argument("--hyper_hid", type=int, default=100, help="hypernet hidden dim")
    parser.add_argument("--gpu", type=int, default=4, help="gpu device ID")
    parser.add_argument("--eval_every", type=int, default=50, help="eval every X selected epochs")
    parser.add_argument("--save_path", type=str, default="/home/ancarey/FairFLHN/experiments/adult/results", help="dir path for output file")
    parser.add_argument("--seed", type=int, default=0, help="seed value")
    parser.add_argument("--fair", type=str, default="both", choices=["none", "eo", "dp", "both"], help="whether to use fairness of not.")
    parser.add_argument("--alpha", type=int, default=50, help="fairness/accuracy trade-off parameter")
    args = parser.parse_args()
    assert args.gpu <= torch.cuda.device_count()
    set_logger()


    device = get_device(gpus=args.gpu)

    args.classes_per_node = 2

    train(writer, data_name=args.data_name,
    model_name=args.model_name,
    classes_per_node = args.classes_per_node,
    num_nodes=args.num_nodes,
    steps=args.num_steps,
    inner_steps=args.inner_steps,
    lr = args.lr,
    inner_lr = args.inner_lr,
    wd = args.wd,
    inner_wd = args.inner_wd,
    hyper_hid = args.hyper_hid,
    n_hidden = args.n_hidden,
    bs = args.batch_size,
    device = device,
    eval_every = args.eval_every,
    alpha = args.alpha,
    fair = args.fair)

    # config = {
    #     'inner-lr': tune.choice([1e-4, 3e-4, 5e-4, 8e-4, 1e-3, 3e-3, 5e-3, 8e-3, 1e-2, 3e-2, 5e-2, 8e-2, 1e-1]),
    #     "h-lr": tune.choice([1e-5, 3e-5, 5e-5, 8e-5, 1e-4, 3e-4, 5e-4, 8e-4, 1e-3, 3e-3, 5e-3, 8e-3, 1e-2, 3e-2, 5e-2]),
    #     'inner-wd': tune.choice([1e-10, 1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3]),
    #     'wd': tune.choice([1e-10, 1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3]),
    #     "alpha": tune.choice([1, 2, 5, 10, 15, 20, 25, 30, 25, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100])
    # }
    #
    # scheduler = ASHAScheduler(
    #     metric="SPD",
    #     mode="min",
    #     max_t=max_num_epochs,
    #     grace_period=1,
    #     reduction_factor=2
    # )
    #
    # reporter = CLIReporter(
    #     max_report_frequency=30,
    #     max_progress_rows = num_samples,
    #     metric_columns=["accuracy", "SPD"],
    #     metric="SPD",
    #     mode="min"
    # )
    #
    # result = tune.run(
    #     partial(train),
    #     resources_per_trial={"gpu": gpus_per_trial},
    #     config=config,
    #     num_samples=num_samples,
    #     scheduler=scheduler,
    #     progress_reporter=reporter
    # )
    #
    # best_trial = result.get_best_trial("SPD", "min", "last")
    # print("Best trial config: {}".format(best_trial.config))
    # print("Best trial final validation accuracy: {}".format(best_trial.last_result["accuracy"]))
    # print("Best trial SPD: {0:1.3f}".format(best_trial.last_result["SPD"]))
    # best_trial_acc = result.get_best_trial("accuracy", "max", "last")
    # print("Best trial config: {}".format(best_trial_acc.config))
    # print("Best trial final validation accuracy: {}".format(best_trial_acc.last_result["accuracy"]))
    # print("Best trial SPD: {0:1.3f}".format(best_trial_acc.last_result["SPD"]))

if __name__ == "__main__":
    # You can change the number of GPUs per trial here:
    main(num_samples=200, max_num_epochs=1, gpus_per_trial=.1)