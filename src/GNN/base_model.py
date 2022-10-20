# !/usr/bin/env python
# -*-coding:utf-8 -*-
# File       : base_model.py
# Author     ：Clark Wang
# version    ：python 3.x
import glob
import torch
import random
import pandas as pd
import numpy as np
from tqdm import tqdm, trange
from torch.nn import functional
from torch_geometric.nn import GCNConv, GATv2Conv, SAGEConv, SuperGATConv
from sklearn.utils import shuffle
from layers import *
from utils import *
from sklearn.model_selection import train_test_split
from GNNLayers import SelectGAT



class BaseModel(torch.nn.Module):
    def __init__(self, args):
        super(BaseModel, self).__init__()
        self.args = args
        self.in_channels = args.feature_length
        self.device = args.device
        filters = self.args.filters.split('_')
        self.gcn_filters = [int(n_filter) for n_filter in filters]
        self.gcn_numbers = len(self.gcn_filters)
        self.gcn_last_filter = self.gcn_filters[-1]
        self.args.final_filter = self.gcn_last_filter
        gcn_parameters = [dict(in_channels=self.gcn_filters[i - 1], out_channels=self.gcn_filters[i]) for i
                          in range(1, self.gcn_numbers)]
        gcn_parameters.insert(0, dict(in_channels=self.in_channels, out_channels=self.gcn_filters[0]))

        self.conv_layer_dict = {
            "gcn": dict(constructor=GCNConv, kwargs=gcn_parameters),
            "gat": dict(constructor=GATv2Conv, kwargs=gcn_parameters),
            "supergat": dict(constructor=SuperGATConv, kwargs=gcn_parameters),
            "sage": dict(constructor=SAGEConv, kwargs=gcn_parameters),
            "selectgat": dict(constructor=SelectGAT, kwargs=gcn_parameters)
        }
        print(gcn_parameters)
        self.setup_layers()

    def calculate_bottleneck_features(self):
        """
        Deciding the shape of the bottleneck layer.
        """
        if self.args.histogram == True:
            self.feature_count = self.args.tensor_neurons + self.args.bins
        else:
            self.feature_count = self.args.tensor_neurons


    def setup_layers(self):
        self.calculate_bottleneck_features()
        conv = self.conv_layer_dict[self.args.conv]
        constructor = conv['constructor']
        print(constructor)
        setattr(self, 'gc{}'.format(1), constructor(**conv['kwargs'][0]))
        for i in range(1, self.gcn_numbers):
            setattr(self, 'gc{}'.format(i + 1), constructor(**conv['kwargs'][i]))

        self.attention = AttentionModule(self.args)
        self.tensor_network = TenorNetworkModule(self.args)

        self.fully_connected_first = torch.nn.Linear(self.feature_count,
                                                     self.args.mlp_neurons)
        self.scoring_layer = torch.nn.Linear(self.args.mlp_neurons, 1)

    def calculate_histogram(self, abstract_features_1, abstract_features_2):
        """
        Calculate histogram from similarity matrix.
        :param abstract_features_1: Feature matrix for graph 1.
        :param abstract_features_2: Feature matrix for graph 2.
        :return hist: Histsogram of similarity scores.
        """
        scores = torch.mm(abstract_features_1, abstract_features_2).detach()
        scores = scores.view(-1, 1)
        if torch.any(torch.isnan(scores)):
            # print(scores)
            scores = torch.where(torch.isnan(scores), torch.full_like(scores, 0), scores)
        hist = torch.histc(scores, bins=self.args.bins)
        hist = hist/torch.sum(hist)
        hist = hist.view(1, -1)
        return hist

    def convolutional_pass(self, adj, feat_in):
        for i in range(1, self.gcn_numbers + 1):
            feat_out = functional.relu(getattr(self, 'gc{}'.format(i))(feat_in, adj),
                                       inplace=True)
            feat_out = functional.dropout(feat_out, p=self.args.dropout, training=True)
            feat_in = feat_out
        return feat_out

    def forward(self, data):
        edge_index_1 = data["edge_index_1"]
        edge_index_2 = data["edge_index_2"]
        features_1 = data["features_1"]
        features_2 = data["features_2"]
        abstract_features_1 = self.convolutional_pass(edge_index_1, features_1)

        abstract_features_2 = self.convolutional_pass(edge_index_2, features_2)
        pooled_features_1 = self.attention(abstract_features_1)
        pooled_features_2 = self.attention(abstract_features_2)

        scores = self.tensor_network(pooled_features_1, pooled_features_2)
        scores = torch.t(scores)
        if self.args.histogram == True:
            hist = self.calculate_histogram(abstract_features_1,
                                            torch.t(abstract_features_2))

            scores = torch.cat((scores, hist), dim=1).view(1, -1)

        scores = torch.nn.functional.normalize(self.fully_connected_first(scores))
        score = torch.nn.functional.relu(self.scoring_layer(scores))
        return score


class BaseTrainer(object):
    def __init__(self, args):
        self.args = args
        self.embedding_len = args.feature_length
        self.get_pairs()
        self.setup_model()

    def setup_model(self):
        self.model = BaseModel(self.args).to(self.args.device)

    def get_pairs(self):
        # data = glob.glob(self.args.data_path + '*.pt')
        data = pd.read_csv(self.args.score_path)
        self.training_pairs, self.testing_pairs = train_test_split(data, test_size=0.2, random_state=42)
        self.training_pairs, self.validation_pairs = train_test_split(self.training_pairs, test_size=0.2, random_state=42)

    def create_batches(self):
        """
        Creating batches from the training graph list.
        :return batches: List of lists with batches.
        """
        # random.shuffle(self.training_pairs)
        batches = []
        for graph in range(0, len(self.training_pairs), self.args.batch_size):
            batches.append(self.training_pairs[graph:graph+self.args.batch_size])
        return batches

    def transfer_to_torch(self, data):
        '''
        :param data: data.series from Score.csv
        :return: graph pair as dict
        '''
        new_dict = {}
        graph_1 = process_pair(self.args.data_path + data['graph_1'] + '.pt')
        graph_2 = process_pair(self.args.data_path + data['graph_2'] + '.pt')
        json_g_1 = load_json(self.args.json_path + data['graph_1'] + '.json')
        json_g_2 = load_json(self.args.json_path + data['graph_2'] + '.json')
        # new_dict['graph_1'], new_dict['graph_2'] = graph_1, graph_2
        new_dict['features_1'] = load_feature(graph_1).to(self.args.device)
        new_dict['features_2'] = load_feature(graph_2).to(self.args.device)
        new_dict['target'] = torch.from_numpy(np.float64(data[self.args.sim_type]).reshape(1, 1)).view(-1).float().to(self.args.device)
        # new_dict['target'] = torch.from_numpy(none_linear_func(self.args.func, data[self.args.sim_type]).reshape(1, 1)).view(-1).float().to(self.args.device)
        # new_dict['target'] = data[self.args.sim_type]
        edge_1 = torch.LongTensor(format_graph(json_g_1)).to(self.args.device)
        edge_2 = torch.LongTensor(format_graph(json_g_2)).to(self.args.device)
        new_dict['edge_index_1'], new_dict['edge_index_2'] = edge_1, edge_2
        return new_dict

    def process_batch(self, batch):
        self.optimizer.zero_grad()
        losses = 0
        for _, graph_pairs in batch.iterrows():
            data = self.transfer_to_torch(graph_pairs)
            target = data['target']
            # data = data.to(self.device)
            prediction = self.model(data).view(1)
            # prediction = torch.from_numpy(np.float64(none_linear_func(self.args.func, prediction)).reshape(1, 1))
            # print(type(prediction))
            # print(target)
            losses = losses + torch.nn.functional.mse_loss(target, prediction)
        losses.backward(retain_graph=True)
        self.optimizer.step()
        loss = losses.item()
        return loss

    def fit(self):
        self.training_loss = []

        self.optimizer = torch.optim.Adam(self.model.parameters(),
                                          lr=self.args.learning_rate,
                                          weight_decay=self.args.weight_decay)
        self.model.train()
        epochs = trange(self.args.epochs, leave=True, desc="Epoch")

        for epoch in epochs:
            last_loss = float('inf')
            patience = self.args.patience
            trigger_times = 0
            batches = self.create_batches()
            self.loss_sum = 0
            main_index = 0

            for index, batch in tqdm(enumerate(batches), total=len(batches), desc="Batches"):
                file = open(self.args.save_path + f'training_{epoch}.txt', 'w')
                loss_score = self.process_batch(batch)
                main_index = main_index + len(batch)
                self.loss_sum = self.loss_sum + loss_score * len(batch)
                loss = self.loss_sum / main_index
                s = str(loss) + '\n'
                file.write(s)
                self.training_loss.append(loss)
                epochs.set_description(f"Epoch:{epoch} Batch:{index} (Loss=%g)" % round(loss, 5))
                if loss > last_loss:
                    trigger_times += 1

                    print('Trigger Times:', trigger_times)
                    last_loss = loss
                    if trigger_times >= patience:
                        print(f"Oh, Stopped at {epoch} epoches, {index} batches, so sad!")
                        break
                else:
                    last_loss = loss
            self.val_score = []
            val_file = open(self.args.save_path + f"val_{epoch}.txt", "w")
            for _, row in self.validation_pairs.iterrows():
                val_data = self.transfer_to_torch(row)
                prediction = self.model(val_data).item()
                val_curr_score = calculate_loss(prediction, val_data['target'].item())
                self.val_score.append(val_curr_score)
                val_file.write(str(val_curr_score) + '\n')
            val_met = np.mean(self.val_score)
            val_file.write(str(val_met)+'\n')
            if self.args.save_path:
                self.save(self.args.save_path + f'epoch_{epoch}.pt')

    def score(self):
        print("\n\nModel evaluation.\n")
        self.model.eval()
        self.scores = []
        self.ground_truth = []
        for _, row in self.testing_pairs.iterrows():
            data = self.transfer_to_torch(row)
            self.ground_truth.append(data['target'].item())
            prediction = self.model(data).item()
            self.scores.append(calculate_loss(prediction, data['target'].item()))
        self.print_evaluation()

    def print_evaluation(self):
        """
        Printing the error rates.
        """
        norm_ged_mean = np.mean(self.ground_truth)
        base_error = np.mean([(n - norm_ged_mean) ** 2 for n in self.ground_truth])
        model_error = np.mean(self.scores)
        print("\nBaseline error: " + str(round(base_error, 5)) + ".")
        print("\nModel test error: " + str(round(model_error, 5)) + ".")

    def save(self, path):
        torch.save(self.model.state_dict(), path)

    def load(self):
        self.model.load_state_dict(torch.load(self.args.load_path))







