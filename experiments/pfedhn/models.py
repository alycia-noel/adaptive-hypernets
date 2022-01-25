from collections import OrderedDict
import torch
import torch.nn.functional as F
from torch import nn

# This class is the architecture for the hypernetwork
# embedding_dim is default to 12
# n_hidden is default to 3

# Don't need to change much here, just the sizes of the layers and the number of them, also the in_channels and so on
class CNNHyper(nn.Module):
    def __init__(
            self, n_nodes, embedding_dim, in_channels=3, out_dim=10, n_kernels=16, hidden_dim=100, n_hidden=1):
        super().__init__()

        self.in_channels = in_channels
        self.out_dim = out_dim
        self.n_kernels = n_kernels

        # The nn.Embedding function generates a lookup table that stores embeddings of a fixed dictionary and size
        # This module is often used to store word embeddings and retrieve them using indices. The input to the
        # module is a list of indices, and the output is the corresponding word embeddings.
        # num_embeddings: the size of the dictionary of embeddings
        # embedding_dim: the size of each embedding vector
        self.embeddings = nn.Embedding(num_embeddings=n_nodes, embedding_dim=embedding_dim) # shape [50, 13]

        # create the number of needed layers, so initial linear, activation functions (ReLU), and the needed hidden
        # linear layers
        layers = [
             nn.Linear(embedding_dim, hidden_dim),
        ]
        for _ in range(n_hidden):
            layers.append(nn.ReLU(inplace=True))
            layers.append(
                nn.Linear(hidden_dim, hidden_dim),
            )

        # create the entire mlp from the above layers
        self.mlp = nn.Sequential(*layers)

        # Generating a way to save the weights and biases by creating linear layers of the right size so that they
        # can be passed to the clients to load into their network
        self.fc1_weights = nn.Linear(hidden_dim, 32 * 32 * 3 + 32)
        self.fc1_bias = nn.Linear(n_hidden)
        self.fc2_weights = nn.Linear(hidden_dim, hidden_dim)
        self.fc2_bias = nn.Linear(n_hidden)
        self.out_weights = nn.Linear(hidden_dim, 10)
        self.out_bias = nn.Linear(10)


        # self.c1_weights = nn.Linear(hidden_dim, self.n_kernels * self.in_channels * 5 * 5)
        # self.c1_bias = nn.Linear(hidden_dim, self.n_kernels)
        # self.c2_weights = nn.Linear(hidden_dim, 2 * self.n_kernels * self.n_kernels * 5 * 5)
        # self.c2_bias = nn.Linear(hidden_dim, 2 * self.n_kernels)
        # self.l1_weights = nn.Linear(hidden_dim, 120 * 2 * self.n_kernels * 5 * 5)
        # self.l1_bias = nn.Linear(hidden_dim, 120)
        # self.l2_weights = nn.Linear(hidden_dim, 84 * 120)
        # self.l2_bias = nn.Linear(hidden_dim, 84)
        # self.l3_weights = nn.Linear(hidden_dim, self.out_dim * 84)
        # self.l3_bias = nn.Linear(hidden_dim, self.out_dim)

    # Do a forward pass
    def forward(self, idx):
        # Get the embedding for client i (referenced by idx), the idx can be 1 through 50
        emd = self.embeddings(idx) # shape [1, 32]

        # Generate the weight output features by passing the embedding through the hypernetwork mlp
        features = self.mlp(emd) # shape [1, 100]

        weights = OrderedDict({
            "fc1.weight": self.fc1_weights(features),
            "fc1.bias": self.fc1_bias(features).view(-1),
            "fc2.weight": self.fc2_weights(features),
            "fc2.bias": self.fc2_bias(features).view(-1),
            "out.weight": self.out_weights(features),
            "out.bias": self.out_bias(features).view(-1),
        })

        # Created an ordered dictionary for all the weights and biases so the client can load them
        # weights = OrderedDict({
        #     "conv1.weight": self.c1_weights(features).view(self.n_kernels, self.in_channels, 5, 5),
        #     "conv1.bias": self.c1_bias(features).view(-1),
        #     "conv2.weight": self.c2_weights(features).view(2 * self.n_kernels, self.n_kernels, 5, 5),
        #     "conv2.bias": self.c2_bias(features).view(-1),
        #     "fc1.weight": self.l1_weights(features).view(120, 2 * self.n_kernels * 5 * 5),
        #     "fc1.bias": self.l1_bias(features).view(-1),
        #     "fc2.weight": self.l2_weights(features).view(84, 120),
        #     "fc2.bias": self.l2_bias(features).view(-1),
        #     "fc3.weight": self.l3_weights(features).view(self.out_dim, 84),
        #     "fc3.bias": self.l3_bias(features).view(-1),
        # })
        # return weights

# Create the CNN architecture that each client uses
class CNNTarget(nn.Module):
    def __init__(self, n_hidden_nodes, keep_rate = 0):  #in_channels=3, n_kernels=16, out_dim=10):
        super(CNNTarget, self).__init__()

        self.n_hidden_nodes = n_hidden_nodes

        if not keep_rate:
            keep_rate = 0.5
        self.keep_rate = keep_rate

        self.fc1 = nn.Linear(32 * 32 * 3 + 32, n_hidden_nodes) #shape of image [3, 32, 32] + context vector [32]
        self.fc1_drop = nn.Dropout(1 - keep_rate)
        self.fc2 = nn.Linear(n_hidden_nodes, n_hidden_nodes)
        self.fc2_drop = nn.Dropout(1 - keep_rate)
        self.out = nn.linear(n_hidden_nodes, 10) # 10 is the number of classes


    def forward(self, x):
        x = nn.functional.relu(self.fc1(x))
        x = self.fc1_drop(x)
        x = nn.functional.relu(self.fc2(x))
        x = self.fc2_drop(x)
        return nn.functional.log_softmax(self.out(x))


        # All but pool will eventually load from the weights and biases generated by the hypernet. This section is
        # simply setting up the structure
        # self.conv1 = nn.Conv2d(in_channels, n_kernels, 5)
        # self.pool = nn.MaxPool2d(2, 2)
        # self.conv2 = nn.Conv2d(n_kernels, 2 * n_kernels, 5)
        # self.fc1 = nn.Linear(2 * n_kernels * 5 * 5, 120)
        # self.fc2 = nn.Linear(120, 84)
        # self.fc3 = nn.Linear(84, out_dim)

    # Do a forward pass of the client network on data batch x
    # def forward(self, x):
    #     x = self.pool(F.relu(self.conv1(x)))
    #     x = self.pool(F.relu(self.conv2(x)))
    #     x = x.view(x.shape[0], -1)
    #     x = F.relu(self.fc1(x))
    #     x = F.relu(self.fc2(x))
    #     x = self.fc3(x)
    #     return x

# The context network, generates a vector that is passed to the hypernetwork
class ContextNetwork(nn.Module):
    def __init__(self, input_channel = 3072, hidden_size= 200, vector_size = 32):
        super(ContextNetwork, self).__init__()
        self.fc1 = nn.Linear(input_channel, hidden_size)
        self.relu1 = nn.LeakyReLU()
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.relu2 = nn.LeakyReLU()
        self.context = nn.Linear(hidden_size, vector_size)

        self.vector_size = vector_size

    def forward(self,x):
        x = torch.flatten(x, 1) # flatten for processing [64 x 3072]
        hidden1 = self.fc1(x)
        relu1 = self.relu1(hidden1)
        hidden2 = self.fc2(relu1)
        relu2 = self.relu2(hidden2)
        context_vector = self.context(relu2)

        ###### adaptive prediction
        avg_context_vector = torch.mean(context_vector, dim=0)
        prediction_vector = avg_context_vector.expand(len(x), self.vector_size)
        prediction_vector = torch.cat((prediction_vector, x), dim=1) # shape = [64, 3104]

        return context_vector, avg_context_vector, prediction_vector

