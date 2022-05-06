import os
import random
from torch.utils.data import Dataset
import torch.utils.data
from sklearn.preprocessing import LabelEncoder
import pandas as pd
from collections import OrderedDict
from sklearn.model_selection import train_test_split
import numpy as np
from collections import defaultdict
from torch.utils.data.sampler import SubsetRandomSampler

class TabularData(Dataset):
    def __init__(self, X, y):
        assert len(X) == len(y)
        n, m = X.shape
        self.n = n
        self.m = m
        self.X = torch.tensor(X)
        self.y = torch.tensor(y)

    def __len__(self):
        return self.n

    def __getitem__(self, idx):

        return self.X[idx], self.y[idx]



def read_dataset(path, data_types, data_name):
    if data_name == 'adult':
        data = pd.read_csv(
            path,
            names=data_types,
            index_col=None,
            dtype=data_types,
            comment='|',
            skipinitialspace=True,
            na_values={
                'capital_gain':99999,
                'workclass':'?',
                'native_country':'?',
                'occupation':'?',
            },
        )
    elif data_name == 'compas':
        url = 'https://raw.githubusercontent.com/propublica/compas-analysis/master/compas-scores-two-years.csv'
        data = pd.read_csv(url)
    return data

def clean_and_encode_dataset(data, data_name):
    if data_name == 'adult':
        data['income_class'] = data.income_class.str.rstrip('.').astype('category')

        data = data.drop('final_weight', axis=1)

        data = data.drop_duplicates()

        data = data.dropna(how='any', axis=0)

        data.capital_gain = data.capital_gain.astype(int)

        data.replace(['Divorced', 'Married-AF-spouse', 'Married-civ-spouse', 'Married-spouse-absent', 'Never-married', 'Separated', 'Widowed'], ['not married', 'married', 'married', 'married', 'not married', 'not married', 'not married'], inplace = True)
        encoders = {}
        for col in ['workclass', 'education', 'marital_status', 'occupation', 'relationship', 'race', 'sex', 'native_country', 'income_class']:
            encoders[col] = LabelEncoder().fit(data[col])
            data.loc[:, col] = encoders[col].transform(data[col])

    elif data_name == 'compas':
        data = data.loc[data['days_b_screening_arrest'] <= 30]
        data = data.loc[data['days_b_screening_arrest'] >= -30]
        data = data.loc[data['is_recid'] != -1]
        data = data.loc[data['c_charge_degree'] != "O"]
        data = data.loc[data['score_text'] != 'N/A']
        #data['race'].loc[data['race'] != "Caucasian"] = 'Other'
        data['is_med_or_high_risk'] = (data['decile_score'] >= 5).astype(int)
        data['length_of_stay'] = (
                pd.to_datetime(data['c_jail_out']) - pd.to_datetime(data['c_jail_in']))

        #cols = ['age', 'c_charge_degree', 'sex', 'age_cat', 'score_text', 'race', 'priors_count', 'length_of_stay', 'days_b_screening_arrest', 'decile_score', 'two_year_recid']
        cols = ['age', 'c_charge_degree', 'race', 'age_cat', 'score_text', 'sex', 'priors_count', 'length_of_stay', 'days_b_screening_arrest', 'decile_score', 'two_year_recid']

        data = data[cols]

        data['length_of_stay'] /= np.timedelta64(1, 'D')
        data['length_of_stay'] = np.ceil(data['length_of_stay'])

        encoders = {}

        for col in ['sex','race', 'c_charge_degree', 'score_text', 'age_cat']:
            encoders[col] = LabelEncoder().fit(data[col])
            data.loc[:, col] = encoders[col].transform(data[col])

    return data

def get_dataset(data_name, num_clients):
    if data_name == 'adult':

        CURRENT_DIR = os.path.abspath(os.path.dirname(__name__))
        TRAIN_DATA_FILE = os.path.join(CURRENT_DIR, 'adult.data')
        TEST_DATA_FILE = os.path.join(CURRENT_DIR, 'adult.test')

        data_types = OrderedDict([
            ("age", "int"),
            ("workclass", "category"),
            ("final_weight", "int"),
            ("education", "category"),
            ("education_num", "int"),
            ("marital_status","category"),
            ("occupation", "category"),
            ("relationship", "category"),
            ("race" ,"category"),
            ("sex", "category"),
            ("capital_gain", "float"),
            ("capital_loss", "int"),
            ("hours_per_week", "int"),
            ("native_country", "category"),
            ("income_class", "category"),
        ])

        train = clean_and_encode_dataset(read_dataset(TRAIN_DATA_FILE, data_types, 'adult'), 'adult')  #(32561, 14)
        test = clean_and_encode_dataset(read_dataset(TEST_DATA_FILE, data_types, 'adult'), 'adult')    #(16281, 14)

        train_set_1 = train[train['workclass'] == 1] #2009, 1004 and 1005
        train_set_2 = train[train['workclass'] != 1] # 24748, 12374 per person

        test_set_1 = test[test['workclass'] == 1] #1016, 508 per person
        test_set_2 = test[test['workclass'] != 1] #13034, 6517 per person

        cols = train_set_1.columns

        features, decision = cols[:-1], cols[-1]

        d_train_g_1 = train_set_1.sample(frac=1).reset_index(drop=True)
        d_train_g_2 = train_set_2.sample(frac=1).reset_index(drop=True)
        d_test_g_1 = test_set_1.sample(frac=1).reset_index(drop=True)
        d_test_g_2 = test_set_2.sample(frac=1).reset_index(drop=True)

        train_sets = [d_train_g_1, d_train_g_2]
        test_sets = [d_test_g_1, d_test_g_2]

        d_train_all = pd.concat(train_sets)
        d_test_all = pd.concat(test_sets)

        return d_train_all, d_test_all, len(d_train_g_1), len(d_test_g_1), features, decision

    elif data_name == 'compas':
        train = clean_and_encode_dataset(read_dataset(None, None, 'compas'), 'compas')
        client_1 = train[train['age'] <= 31]  # 3164

        client_2 = train[train['age'] > 31]  # 3008

        cols = client_1.columns

        features, decision = cols[:-1], cols[-1]

        d_train_g_1, d_test_g_1 = train_test_split(client_1, test_size=300)
        d_train_g_2, d_test_g_2 = train_test_split(client_2, test_size=300)


        d_train_g_1 = d_train_g_1.sample(frac=1).reset_index(drop=True)
        d_train_g_2 = d_train_g_2.sample(frac=1).reset_index(drop=True)
        d_test_g_1 = d_test_g_1.sample(frac=1).reset_index(drop=True)
        d_test_g_2 = d_test_g_2.sample(frac=1).reset_index(drop=True)

        train_sets = [d_train_g_1, d_train_g_2]
        test_sets = [d_test_g_1, d_test_g_2]

        d_train_all = pd.concat(train_sets)
        d_test_all = pd.concat(test_sets)


    return d_train_all, d_test_all, len(d_train_g_1), len(d_test_g_1), len(d_train_g_2), len(d_test_g_2), features, decision


def gen_random_loaders(data_name, num_users, bz, classes_per_user):
    loader_params = {"batch_size": bz, "shuffle": False, "pin_memory": True, "num_workers": 0}

    dataloaders = []

    group_all_train_data, group_all_test_data, len_d_train_g_1, len_d_test_g_1, len_d_train_g_2, len_d_test_g_2, features, decision = get_dataset(data_name, num_users)

    datasets = [group_all_train_data, group_all_test_data]


    for i, d in enumerate(datasets):
        usr_subset_idx = [[] for i in range(num_users)]

        if i == 0: # Train
            for usr_i in range(num_users):
                if usr_i == 0:
                    usr_subset_idx[usr_i].extend(TabularData(d[0:int(len_d_train_g_1/2)][features].values, d[0:int(len_d_train_g_1/2)][decision].values))
                elif usr_i == 1:
                    usr_subset_idx[usr_i].extend(TabularData(d[int(len_d_train_g_1/2):len_d_train_g_1][features].values, d[int(len_d_train_g_1/2):len_d_train_g_1][decision].values))
                elif usr_i == 2:
                    usr_subset_idx[usr_i].extend(TabularData(d[len_d_train_g_1: (len_d_train_g_1 + int(len_d_train_g_2/2))][features].values, d[len_d_train_g_1: (len_d_train_g_1 + int(len_d_train_g_2/2))][decision].values))
                elif usr_i == 3:
                    usr_subset_idx[usr_i].extend(TabularData(d[len_d_train_g_1 +int(len_d_train_g_2/2):][features].values, d[int(len_d_train_g_1 +len_d_train_g_2/2):][decision].values))

            subsets = list(usr_subset_idx)
            loader_params['shuffle'] = True
            dataloaders.append(list(map(lambda x: torch.utils.data.DataLoader(x, **loader_params), subsets)))

        elif i == 1: # Test
            for usr_i in range(num_users):
                if usr_i == 0:
                    usr_subset_idx[usr_i].extend(TabularData(d[0:int(len_d_test_g_1 / 2)][features].values,
                                                             d[0:int(len_d_test_g_1 / 2)][decision].values))
                elif usr_i == 1:
                    usr_subset_idx[usr_i].extend(
                        TabularData(d[int(len_d_test_g_1 / 2):len_d_test_g_1][features].values,
                                    d[int(len_d_test_g_1 / 2):len_d_test_g_1][decision].values))
                elif usr_i == 2:
                    usr_subset_idx[usr_i].extend(
                        TabularData(d[len_d_test_g_1: (len_d_test_g_1 + int(len_d_test_g_2 / 2))][features].values,
                                    d[len_d_test_g_1: (len_d_test_g_1 + int(len_d_test_g_2 / 2))][decision].values))
                elif usr_i == 3:
                    usr_subset_idx[usr_i].extend(TabularData(d[len_d_test_g_1 + int(len_d_test_g_2 / 2):][features].values,d[int(len_d_test_g_1 + len_d_test_g_2 / 2):][decision].values))

            subsets = list(usr_subset_idx)

            dataloaders.append(list(map(lambda x: torch.utils.data.DataLoader(x, **loader_params), subsets)))

    return dataloaders, features


