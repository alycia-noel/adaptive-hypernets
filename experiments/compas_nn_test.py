import time
import warnings
import torch
import torch.nn as nn
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from torch.utils.data import Dataset, DataLoader
import torch.nn.functional as F
from sklearn.metrics import roc_curve, auc, confusion_matrix

warnings.filterwarnings("ignore")

class TabularData(Dataset):
    def __init__(self, X, y):
        assert len(X) == len(y)
        n, m = X.shape
        self.n = n
        self.m = m
        self.X = torch.tensor(X, dtype=torch.float64)
        self.y = torch.tensor(y, dtype=torch.float64)

    def __len__(self):
        return self.n

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

class NN(nn.Module):
    def __init__(self, input_size, hidden_sizes=[64,64,32], dropout_rate = .35):
        super(NN, self).__init__()
        self.input_size = input_size
        self.dropout_rate = dropout_rate

        self.fc1 = nn.Linear(self.input_size, hidden_sizes[0]) #[8, 64]
        self.fc2 = nn.Linear(hidden_sizes[0], hidden_sizes[1]) #[64, 64]
        self.fc3 = nn.Linear(hidden_sizes[1], hidden_sizes[2]) #[64, 32]
        self.fc4 = nn.Linear(hidden_sizes[2], 1) #[32, 1]

        self.dropout = nn.Dropout(self.dropout_rate)

    def forward(self, data):
        x1 = F.relu(self.fc1(data))
        x2 = F.relu(self.fc2(x1))
        x3 = self.dropout(x2)
        x4 = F.relu(self.fc3(x3))
        x5 = self.dropout(x4)
        x6 = self.fc4(x5)
        out = torch.sigmoid(x6)

        return out


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
    plt.title('ROC for NN on COMPAS')
    #if fname is not None:
    #    plt.savefig(fname)
    #else:
    plt.show()

# Import the data and visualize it (if you want using df.info())
# decile_score = risk score prediction
torch.manual_seed(0)
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

cols = ['age', 'c_charge_degree', 'race', 'age_cat', 'score_text', 'sex', 'priors_count', 'length_of_stay','days_b_screening_arrest', 'decile_score', 'is_recid', 'two_year_recid']
compas = df_filtered[cols]

compas['length_of_stay'] /= np.timedelta64(1, 'D')
compas['length_of_stay'] = np.ceil(compas['length_of_stay'])

cols = compas.columns
features, decision = cols[:-1], cols[-1]

encoders = {}
for col in ['race', 'sex', 'c_charge_degree', 'score_text', 'age_cat']:
    encoders[col] = LabelEncoder().fit(compas[col])
    compas.loc[:, col] = encoders[col].transform(compas[col])

results = []

d_train, d_test = train_test_split(compas, test_size=500)

data_train = TabularData(d_train[features].values, d_train[decision].values)
data_test = TabularData(d_test[features].values, d_test[decision].values)

model = NN(input_size=11)
model = model.double()

train_loader = DataLoader(data_train, shuffle = True, batch_size = 16)
test_loader = DataLoader(data_test, shuffle = False, batch_size= 16)

optimizer = torch.optim.SGD(model.parameters(), lr=1.e-4, momentum=.97, weight_decay=1.e-5)
#optimizer = torch.optim.Adam(model.parameters(), lr = 2.e-4, weight_decay = 0)
loss = nn.BCELoss(reduction='none') #Binary Cross Entropy loss
no_batches = len(train_loader)
loss_values = []
test_loss_values = []
acc_values = []
test_acc =[]
test_error = []
tp = []
tn = []
fp = []
fn = []
times = []

# Train model
model.train()
torch.cuda.synchronize()
for epoch in range(100):
    start = time.time()
    running_loss = 0.0
    correct = 0.0
    total = 0.0
    for i, (x, y) in enumerate(train_loader):
        optimizer.zero_grad()
        y_ = model(x)
        err = loss(y_.flatten(), y)
        err = err.mean()
        running_loss += err.item() * x.size(0)
        err.backward()
        optimizer.step()

        classes = torch.argmax(y_, dim=1)

        for i in classes:
            if float(classes[i].item()) == y[i].item():
                correct = correct + 1

    accuracy = (100 * correct / len(data_train))
    loss_values.append(running_loss / len(train_loader))

    torch.cuda.synchronize()
    end = time.time()
    elapsed = end - start
    times.append(elapsed)
    print('Epoch: {0}/{1};\t Loss: {2:1.3f};\tAcc:{3:1.3f};\tTime:{4:1.2f}'.format(epoch + 1, 100, running_loss / len(train_loader), accuracy, elapsed))

    total_time = sum(times)

    # Eval Model
    model.eval()
    predictions = []
    running_loss_test = 0
    with torch.no_grad():
        for i, (x, y) in enumerate(test_loader):
            pred = model(x)
            predictions.extend(pred.flatten().numpy())
            test_err = loss(pred.flatten(), y)
            test_err = test_err.mean()
            running_loss_test += test_err.item() * x.size(0)

    test_loss_values.append(running_loss_test / len(test_loader))


    if epoch == 99 or (epoch % 49 == 0 and epoch != 0):
        plt.plot(loss_values, label='Train Loss')
        plt.plot(test_loss_values, label='Test Loss')
        plt.xlabel('Epoch')
        plt.ylabel('Loss')
        plt.title('Loss over Epochs for NN Model on COMPAS')
        plt.legend(loc="upper right")
        plt.show()
        # if i % 354 == 0:

    my_rounded_list = [round(elem) for elem in predictions]
    CM = confusion_matrix(data_test.y, my_rounded_list)

    TN = CM[0][0]
    FN = CM[1][0]
    TP = CM[1][1]
    FP = CM[0][1]

    accuracy = (TP + TN) / (TP + FP + FN + TN)
    error = (FP + FN) / (TP + FP + FN + TN)
    res = (
        pd  .DataFrame(columns = features, index = d_test.index)
            .add_suffix('_partial')
            .join(d_test)
            .assign(prediction=predictions)
            .assign(round=epoch)
    )

    results.append(res)
    test_acc.append(accuracy)
    test_error.append(error)
    tn.append(TN)
    tp.append(TP)
    fn.append(FN)
    fp.append(FP)

results = pd.concat(results)
average_test_acc = sum(test_acc) / len(test_acc)
print(test_acc, test_acc[0], test_acc[len(test_acc) - 1], )
print('Test Accuracy: ',test_acc[0], test_acc[len(test_acc) - 1], average_test_acc)
print('Test Error: ', test_error[0], test_error[len(test_error) - 1])
print('Train Time: ', total_time)
print('TP: ', tp[len(tp)-1], 'TN: ', tn[len(tn)-1], 'FP: ', fp[len(fp)-1], 'FN: ', fn[len(fn)-1])
for col, encoder in encoders.items():
        results.loc[:,col] = encoder.inverse_transform(results[col])


plot_roc_curves(results, 'prediction', 'two_year_recid', size=(7, 5), fname='./results/roc.png')