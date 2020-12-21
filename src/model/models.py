import numpy as np
import torch
import torch.nn as nn
import torch_geometric.nn as gnn
import torch.nn.functional as F

from model import model_utils


######################################################################
################################# DQN ################################
######################################################################

class DQN(nn.Module):
    '''
    Vanilla deep Q-Network
    '''

    def __init__(self, state_size, action_size, hidden_sizes=[128, 128], nonlinearity="tanh"):
        super(DQN, self).__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.fc = model_utils.get_linear(
            state_size, action_size, hidden_sizes, nonlinearity=nonlinearity
        )

    def forward(self, state):
        state = torch.flatten(state, start_dim=1)
        return self.fc(state)


######################################################################
########################### Dueling DQN ##############################
######################################################################

class DuelingDQN(nn.Module):
    '''
    Dueling DQN
    '''

    def __init__(self, state_size, action_size, hidden_sizes=[128, 128], nonlinearity="tanh", aggregation="mean"):
        super(DuelingDQN, self).__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.aggregation = aggregation
        self.fc_val = model_utils.get_linear(
            state_size, 1, hidden_sizes, nonlinearity=nonlinearity
        )
        self.fc_adv = model_utils.get_linear(
            state_size, action_size, hidden_sizes, nonlinearity=nonlinearity
        )

    def forward(self, state):
        state = torch.flatten(state, start_dim=1)
        val = self.fc_val(state)
        adv = self.fc_adv(state)
        agg = adv.mean() if self.aggregation == "mean" else adv.max()
        return val + adv - agg


######################################################################
##################### Single agent DQN + GNN #########################
######################################################################

class SingleDQNGNN(DQN):
    '''
    Single agent DQN + GNN
    '''

    def __init__(self, state_size, action_size, pos_size, embedding_size,
                 hidden_sizes=[128, 128], nonlinearity="tanh",
                 gnn_hidden_size=16, depth=3, dropout=0.0):
        super(SingleDQNGNN, self).__init__(
            action_size * embedding_size, action_size,
            hidden_sizes=hidden_sizes, nonlinearity=nonlinearity
        )
        self.pos_size = pos_size
        self.embedding_size = embedding_size
        self.depth = depth
        self.dropout = dropout
        self.nl = nn.ReLU() if nonlinearity == "relu" else nn.Tanh()
        self.gnn_conv = nn.ModuleList()
        self.gnn_conv.append(gnn.GCNConv(
            state_size, gnn_hidden_size
        ))
        for i in range(1, self.depth - 1):
            self.gnn_conv.append(gnn.GCNConv(
                gnn_hidden_size, gnn_hidden_size
            ))
        self.gnn_conv.append(gnn.GCNConv(
            gnn_hidden_size, self.embedding_size
        ))

    def forward(self, state):
        graphs = state.to_data_list()
        embs = torch.empty(
            size=(
                len(graphs),
                self.pos_size,
                self.embedding_size
            ), dtype=torch.float
        )

        # For each graph in the batch
        for i, graph in enumerate(graphs):
            x, edge_index, edge_weight, pos = (
                graph.x, graph.edge_index, graph.edge_weight, graph.pos
            )

            # Perform a number of graph convolutions specified by
            # the given depth
            for d in range(self.depth):
                x = self.gnn_conv[d](x, edge_index, edge_weight=edge_weight)
                emb = x
                x = self.nl(x)
                x = F.dropout(x, p=self.dropout, training=self.training)

            # Extract useful embeddings
            for j, p in enumerate(pos):
                if p == -1:
                    embs[i, j] = torch.tensor(
                        [-self.depth] * self.embedding_size, dtype=torch.float
                    )
                else:
                    embs[i, j] = emb[p.item()]

        # Call the DQN with a tensor of shape
        # (batch_size, pos_size, embedding_size)
        return super().forward(embs)


######################################################################
###################### Multi agent DQN + GNN #########################
######################################################################

class MultiDQNGNN(DQN):
    '''
    Multi agent DQN + GNN
    '''

    def __init__(self, action_size, input_width, input_height, input_channels, output_channels,
                 hidden_channels=[16, 32, 16], pool=False, embedding_size=128, hidden_sizes=[128, 128],
                 nonlinearity="relu", device="cpu"):
        super(MultiDQNGNN, self).__init__(
            embedding_size, action_size,
            hidden_sizes=hidden_sizes, nonlinearity=nonlinearity
        )
        self.input_width = input_width
        self.input_height = input_height
        self.input_channels = input_channels
        self.output_channels = output_channels
        self.hidden_channels = hidden_channels
        self.embedding_size = embedding_size
        self.device = device

        # Encoder
        self.convs = model_utils.get_conv(
            input_channels, output_channels, hidden_channels,
            kernel_size=3, stride=1, padding=0,
            nonlinearity=nonlinearity, pool=pool
        )

        # MLP
        output_width, output_height = model_utils.conv_block_output_size(
            self.convs, input_width, input_height
        )
        assert output_width > 0 and output_height > 0
        self.mlp = model_utils.get_linear(
            output_width * output_height * output_channels,
            embedding_size, hidden_sizes, nonlinearity=nonlinearity
        )

        # GNN
        self.gnn_conv = gnn.GCNConv(
            embedding_size, embedding_size, add_self_loops=False
        )

    def forward(self, states, adjacencies, inactives):
        q_values = torch.zeros(
            (states.shape[0], states.shape[1], self.action_size),
            dtype=torch.float, device=self.device
        )
        active_indexes = (~inactives).nonzero()
        for batch_number, batch in enumerate(states):
            current_active_indexes = active_indexes[
                active_indexes[:, 0] == batch_number
            ]
            # If every agent is inactive, skip computations
            if current_active_indexes.shape[0] == 0:
                continue

            # Encode the FOV observation of each agent
            # with the convolutional encoder
            encoded = self.convs(batch)

            # Use an MLP from the encoded values to have a
            # consistent number of features
            flattened = torch.flatten(encoded, start_dim=1)
            features = self.mlp(flattened)

            # Create the graph used by the defined GNN conv,
            # specified by the given adjacency matrix
            edge_index, edge_weight = [], []
            num_agents = adjacencies.shape[1]
            for i in range(num_agents):
                for j in range(num_agents):
                    if adjacencies[batch_number, i, j] != 0 or i == j:
                        edge_index.append([i, j])
                        edge_weight.append(adjacencies[batch_number, i, j])
            edge_index = torch.tensor(
                edge_index, dtype=torch.long, device=self.device
            ).t().contiguous()
            edge_weight = torch.tensor(
                edge_weight, dtype=torch.float, device=self.device
            )

            # Compute embeddings for each node by performing graph convolutions
            embeddings = self.gnn_conv(
                features, edge_index, edge_weight=edge_weight
            )

            # Call the DQN with the embeddings associated to active agents
            for ind in current_active_indexes:
                handle = ind[1].item()
                q_values[batch_number, handle, :] = (
                    super().forward(embeddings[handle].unsqueeze(0))
                )

        # Return the Q-values tensor
        return q_values