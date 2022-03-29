import time
import random
import os
import warnings
import json
import torch
import torch.nn as nn
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from torch.utils.data import Dataset, DataLoader
from fairtorch import DemographicParityLoss, EqualiedOddsLoss
from sklearn.metrics import roc_curve, auc, confusion_matrix

warnings.filterwarnings("ignore")

def seed_everything(seed):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    #torch.backends.cudnn.deterministic = True

class TabularData(Dataset):
    def __init__(self, X, y, s):
        assert len(X) == len(y) == len(s)
        n, m = X.shape
        self.n = n
        self.m = m
        self.X = torch.tensor(X, dtype=torch.float64)
        self.y = torch.tensor(y, dtype=torch.float64)
        self.s = torch.tensor(s, dtype=torch.float64)

    def __len__(self):
        return self.n

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx], self.s[idx]

class LR(nn.Module):
    def __init__(self, input_size):
        super(LR, self).__init__()
        self.fc1 = nn.Linear(input_size, 1)

    def forward(self, x, test):
        if test == True:
            y = self.fc1(x)
            y = torch.sigmoid(y)
        else:
            y = self.fc1(x)
        return y


def plot_roc_curves(results, pred_col, resp_col, size=(7, 5), fname=None):
    plt.clf()
    plt.style.use('classic')
    plt.figure(figsize=size)

    for _, res in results.groupby('round'):
        fpr, tpr, _ = roc_curve(res[resp_col], res[pred_col])
        roc_auc = auc(fpr, tpr)
        plt.plot(fpr, tpr, '-', color='orange', lw=0.5)

    fpr, tpr, _ = roc_curve(results[resp_col], results[pred_col])
    roc_auc = auc(fpr, tpr)
    plt.plot(fpr, tpr, '-', color='darkorange', lw=1.5, label='ROC curve (area = %0.2f)' % roc_auc, )
    plt.plot([0, 1], [0, 1], color='navy', lw=1.5, linestyle='--')
    plt.xlim([0.0, 1.0])
    plt.ylim([0.0, 1.05])
    plt.grid()
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.legend(loc="lower right")
    plt.title('ROC for LR on COMPAS')
    #if fname is not None:
    #    plt.savefig(fname)
    #else:
    plt.show()

# Import the data and visualize it (if you want using df.info())
# decile_score = risk score prediction
seed_everything(42)
url = 'https://raw.githubusercontent.com/propublica/compas-analysis/master/compas-scores-two-years.csv'
df = pd.read_csv(url)

# Cleaning and parsing the data
# 1. If the charge date of a defendants COMPAS score was not within 30 days from when the person was arrested, we assume that because of data
#    quality reason, that we do not have the right offense
# 2. If is_recid = -1 then there was no COMPAS case found
# 3. c_charge_degree of 'O' will result in no jail time so they are removed
df_filtered = df.loc[df['days_b_screening_arrest'] <= 30]
df_filtered = df_filtered.loc[df_filtered['days_b_screening_arrest'] >= -30]
df_filtered = df_filtered.loc[df_filtered['is_recid'] != -1]
df_filtered = df_filtered.loc[df_filtered['c_charge_degree'] != "O"]
df_filtered = df_filtered.loc[df_filtered['score_text'] != 'N/A']
df_filtered['is_med_or_high_risk']  = (df_filtered['decile_score']>=5).astype(int)
df_filtered['length_of_stay'] = (pd.to_datetime(df_filtered['c_jail_out']) - pd.to_datetime(df_filtered['c_jail_in']))

cols = ['age', 'c_charge_degree', 'race', 'age_cat', 'score_text', 'priors_count', 'length_of_stay','days_b_screening_arrest', 'decile_score', 'two_year_recid', 'sex',]
compas = df_filtered[cols]

compas['length_of_stay'] /= np.timedelta64(1, 'D')
compas['length_of_stay'] = np.ceil(compas['length_of_stay'])

cols = compas.columns
features, decision, sensitive = cols[:-2], cols[-2], cols[-1]

encoders = {}
for col in ['race', 'sex', 'c_charge_degree', 'score_text', 'age_cat']:
    encoders[col] = LabelEncoder().fit(compas[col])
    compas.loc[:, col] = encoders[col].transform(compas[col])

d_train, d_test = train_test_split(compas, test_size=300)

all_results = []

data_train = TabularData(d_train[features].values, d_train[decision].values, d_train[sensitive].values)
data_test = TabularData(d_test[features].values, d_test[decision].values, d_test[sensitive].values)

learning_rate = [.1, .05, .01, .005, .003, .001, .0005, .0003, .0001]
batch_size = [64, 128]
alphas = [5, 10, 15, 20, 25, 30, 50, 70, 90]
epochs = [25, 50, 100, 150, 200]

no_cuda=False
gpus='4'
device = torch.cuda.set_device(4)

for i, lr in enumerate(learning_rate):
    for j, bs in enumerate(batch_size):
        for k, alpha in enumerate(alphas):
            for m, eps in enumerate(epochs):
                mid_results = []
                for iter in range(5):
                    model = LR(input_size=9)
                    model = model.double()
                    model = model.to(device)

                    # for param in model.parameters():
                    #     nn.init.normal_(param, 0, 1e-7)

                    train_loader = DataLoader(data_train, shuffle = True, batch_size = bs) #64
                    test_loader = DataLoader(data_test, shuffle = False, batch_size= bs)

                    optimizer = torch.optim.Adam(model.parameters(), lr = lr, betas=(.9,.999), eps=1e-08) #.005
                    loss = nn.BCEWithLogitsLoss(reduction='mean')   #binary logarithmic loss function
                    dp_loss = DemographicParityLoss(sensitive_classes=[0, 1], alpha=alpha) #20

                    no_batches = len(train_loader)
                    results = []
                    loss_values =[]
                    test_loss_values = []
                    test_error, F_ERR, M_ERR = [], [], []

                    acc_values, test_acc, F_ACC, M_ACC = [], [], [], []

                    tp, tn, fp, fn, f1 = [], [], [], [], []
                    F_TP, F_FP, F_TN, F_FN, F_F1 = [], [], [], [], []
                    M_TP, M_FP, M_TN, M_FN, M_F1 = [], [], [], [], []
                    EOD, SPD, AOD = [], [], []

                    times = []
                    #torch.cuda.amp.autocast(enabled=False)
                    # Train model
                    model.train() #warm-up
                    torch.cuda.synchronize()
                    for epoch in range(eps):
                        start = time.time()
                        running_loss = 0.0
                        correct = 0.0
                        total = 0.0
                        for i, (x, y, s) in enumerate(train_loader):
                            optimizer.zero_grad()
                            y_ = model(x.to(device), test=True)
                            err = loss(y_.flatten(), y.to(device)) + dp_loss(x.float(), y_.float(), s.float())
                            err = err.mean()
                            running_loss += err.item() * x.size(0)
                            err.backward()
                            optimizer.step()

                            preds = y_.round().reshape(1, len(y_))
                            correct += (preds.eq(y)).sum().item()

                        accuracy = (100 * correct / len(data_train))
                        acc_values.append(accuracy)
                        loss_values.append(running_loss / len(train_loader))

                        end = time.time()
                        elapsed = end - start
                        times.append(elapsed)
                        #print('Epoch: {0}/{1};\t Loss: {2:1.3f};\tAcc:{3:1.3f};\tTime:{4:1.2f}'.format(epoch + 1, 50, running_loss / len(train_loader), accuracy, elapsed))

                        total_time = sum(times)

                        # Eval Model
                        model.eval()
                        predictions = []
                        running_loss_test = 0
                        TP, FP, FN, TN = 0, 0, 0, 0
                        f_tp, f_fp, f_tn, f_fn = 0, 0, 0, 0
                        m_tp, m_fp, m_tn, m_fn = 0, 0, 0, 0
                        total = 0.0
                        correct = 0.0
                        with torch.no_grad():
                            for i, (x, y, s) in enumerate(test_loader):
                                pred = model(x.to(device), test=True)
                                test_err = loss(pred.flatten(), y.to(device))
                                test_err = test_err.mean()
                                running_loss_test += test_err.item() * x.size(0)

                                preds = pred.round().flatten()
                                predictions.extend(preds.numpy())
                                correct += (preds.eq(y)).sum().item()

                                predicted_prediction = preds.type(torch.IntTensor).numpy().reshape(-1)
                                labels_pred = y.type(torch.IntTensor).numpy().reshape(-1)

                                for i in range(len(s)):
                                    if s[i].item() == 0:
                                        if predicted_prediction[i] == 1 and labels_pred[i] == 1:
                                            f_tp += 1
                                            TP += 1
                                        elif predicted_prediction[i] == 1 and labels_pred[i] == 0:
                                            f_fp += 1
                                            FP += 1
                                        elif predicted_prediction[i] == 0 and labels_pred[i] == 0:
                                            f_tn += 1
                                            TN += 1
                                        elif predicted_prediction[i] == 0 and labels_pred[i] == 1:
                                            f_fn += 1
                                            FN += 1
                                    else:
                                        if predicted_prediction[i] == 1 and labels_pred[i] == 1:
                                            m_tp += 1
                                            TP += 1
                                        elif predicted_prediction[i] == 1 and labels_pred[i] == 0:
                                            m_fp += 1
                                            FP += 1
                                        elif predicted_prediction[i] == 0 and labels_pred[i] == 0:
                                            m_tn += 1
                                            TN += 1
                                        elif predicted_prediction[i] == 0 and labels_pred[i] == 1:
                                            m_fn += 1
                                            FN += 1

                        test_loss_values.append(running_loss_test / len(test_loader))

                        try:
                            f1_score_prediction = TP / (TP + (FP + FN) / 2)
                            f1_female = f_tp / (f_tp + (f_fp + f_fn) / 2)
                            f1_male = m_tp / (m_tp + (m_fp + m_fn) / 2)

                            f1.append(f1_score_prediction)
                            F_F1.append(f1_female)
                            M_F1.append(f1_male)

                            # if epoch == 49:
                            #     plt.plot(loss_values, label='Train Loss')
                            #     plt.plot(test_loss_values, label='Test Loss')
                            #     plt.xlabel('Epoch')
                            #     plt.ylabel('Loss')
                            #     plt.title('Loss over Epochs for LR Model on COMPAS')
                            #     plt.legend(loc="upper right")
                            #     plt.show()

                            accuracy = (TP + TN) / (TP + FP + FN + TN)
                            f_acc = (f_tp + f_tn) / (f_tp + f_fp + f_fn + f_tn)
                            m_acc = (m_tp + m_tn) / (m_tp + m_fp + m_fn + m_tn)

                            error = (FP + FN) / (TP + FP + FN + TN)
                            f_err = (f_fp + f_fn) / (f_tp + f_fp + f_fn + f_tn)
                            m_err = (m_fp + m_fn) / (m_tp + m_fp + m_fn + m_tn)

                            if f_fp == 0 and f_tn == 0:
                                AOD.append((((f_tp / (f_tp + f_fn)) - (m_tp / (m_tp + m_fn))) + (0 - (m_fp / (m_fp + m_tn)))) / 2)
                            else:
                                AOD.append((((f_tp / (f_tp + f_fn)) - (m_tp / (m_tp + m_fn))) + ((f_fp / (f_fp + f_tn)) - (m_fp / (m_fp + m_tn)))) / 2)  # average odds difference
                            EOD.append((f_tp / (f_tp + f_fn)) - (m_tp / (m_tp + m_fn)))  # equal opportunity difference
                            SPD.append((f_tp + f_fp) / (f_tp + f_fp + f_tn + f_fn) - (m_tp + m_fp) / (m_tp + m_fp + m_tn + m_fn))

                            res = (
                                pd  .DataFrame(columns = features, index = d_test.index)
                                    .add_suffix('_partial')
                                    .join(d_test)
                                    .assign(prediction=predictions)
                                    .assign(round=epoch)
                                )

                        except ZeroDivisionError:
                            continue

                        results.append(res)
                        test_acc.append(accuracy)
                        F_ACC.append(f_acc)
                        M_ACC.append(m_acc)
                        test_error.append(error)
                        F_ERR.append(f_err)
                        M_ERR.append(m_err)

                        tn.append(TN)
                        tp.append(TP)
                        fn.append(FN)
                        fp.append(FP)

                        F_TN.append(f_tn)
                        F_TP.append(f_tp)
                        F_FN.append(f_fn)
                        F_FP.append(f_fp)

                        M_TN.append(m_tn)
                        M_TP.append(m_tp)
                        M_FN.append(m_fn)
                        M_FP.append(m_fp)

                    results = pd.concat(results)
                    average_test_acc = sum(test_acc) / len(test_acc)
                    female_test_acc = sum(F_ACC) / len(F_ACC)
                    male_test_acc = sum(M_ACC) / len(M_ACC)

                    add_to_dict = {'lr': lr, 'bs': bs, 'alpha': alpha, 'epoch': epoch, 'avg_acc': average_test_acc, 'EOD': EOD[len(EOD) - 1], 'AOD': AOD[len(AOD) - 1], 'SPD': SPD[len(SPD) - 1]}
                    mid_results.append(add_to_dict)
                    #print(mid_results)

                all_avg = sum(d['avg_acc'] for d in mid_results) / len(mid_results)
                all_eod = sum(d['EOD'] for d in mid_results) / len(mid_results)
                all_aod = sum(d['AOD'] for d in mid_results) / len(mid_results)
                all_spd = sum(d['SPD'] for d in mid_results) / len(mid_results)

                add_to_all = {'lr': lr, 'bs': bs, 'alpha': alpha, 'epoch': epoch, 'avg_acc': all_avg, 'EOD': all_eod, 'AOD': all_aod, 'SPD': all_spd}
                all_results.append(add_to_all)
                print(add_to_all)

# best_eod = -10000
# best_acc = 0
# best_index = 0
#
# for i in range(len(all_results)):
#     if all_results[i].get('avg_acc') >= best_acc:
#         if abs(all_results[i].get('EOD')) < abs(best_eod):
#             best_acc = all_results[i].get('avg_acc')
#             best_eod = all_results[i].get('EOD')
#             best_index = i

textfile = open("all_results.txt", "w")
for element in all_results:
    textfile.write(json.dumps(all_results))
textfile.close()

#print(all_results[i])



                # print('*******************')
                # print('*       all       *')
                # print('*******************')
                # print('Test Accuracy: {0:1.3f}'.format(test_acc[len(test_acc) - 1]))
                # print('Test Error: {0:1.3f}'.format(test_error[len(test_error) - 1]))
                # print('TP: ', tp[len(tp) - 1], 'FP: ', fp[len(fp) - 1], 'TN: ', tn[len(tn) - 1], 'FN: ', fn[len(fn) - 1])
                # print('EOD: {0:1.4f}'.format(EOD[len(EOD) - 1]))
                # print('SPD: {0:1.4f}'.format(SPD[len(SPD) - 1]))
                # print('AOD: {0:1.4f}'.format(AOD[len(AOD) - 1]))
                # print('F1: {0:1.3f}'.format(f1[len(f1) - 1]))
                # print("")
                # print('**********************')
                # print('*       Female       *')
                # print('**********************')
                # print('Test Accuracy: {0:1.3f}'.format(F_ACC[len(F_ACC) - 1]))
                # print('Test Error: {0:1.3f}'.format(F_ERR[len(F_ERR) - 1]))
                # print('TP: ', F_TP[len(F_TP) - 1], 'FP: ', F_FP[len(F_FP) - 1], 'TN: ', F_TN[len(F_TN) - 1], 'FN: ',
                #       F_FN[len(F_FN) - 1])
                # print('F1: {0:1.3f}'.format(F_F1[len(F_F1) - 1]))
                # print("")
                # print('********************')
                # print('*       Male       *')
                # print('********************')
                # print('Test Accuracy: {0:1.3f}'.format(M_ACC[len(M_ACC) - 1]))
                # print('Test Error: {0:1.3f}'.format(M_ERR[len(M_ERR) - 1]))
                # print('TP: ', M_TP[len(M_TP) - 1], 'FP: ', M_FP[len(M_FP) - 1], 'TN: ', M_TN[len(M_TN) - 1], 'FN: ',
                #       M_FN[len(M_FN) - 1])
                # print('F1: {0:1.3f}'.format(M_F1[len(M_F1) - 1]))
                #
                # print('Train Time: {0:1.2f}'.format(total_time))
                #
                # for col, encoder in encoders.items():
                #     results.loc[:, col] = encoder.inverse_transform(results[col])
                #
                # plot_roc_curves(results, 'prediction', 'two_year_recid', size=(7, 5), fname='./results/roc.png')
