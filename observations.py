'''
'''

from collections import namedtuple

import numpy as np

from flatland.core.env import Environment
from flatland.core.env_observation_builder import ObservationBuilder
from flatland.core.grid.grid_utils import coordinate_to_position
from flatland.utils.ordered_set import OrderedSet
from flatland.envs.rail_env import RailAgentStatus

import utils
from railway_encoding import CellOrientationGraph


'''
Observation:
    - Structure:
        * Tensor of shape (max_depth, max_depth, features), where max_depth 
          is the maximum number of nodes in the packed graph to consider and
          features is the total amount of features for each node
        * The observation contains the features of the nodes in the shortest path
          as the first row and the features of the nodes in the deviation paths
          (which are exactly max_depth - 1) as the following rows
    - Features:
        1. Number of agents (going in my direction) identified in the subpath 
           from the root up to each node in the path
        2. Number of agents (going in a direction different from mine) identified 
           in the subpath from the root up to each node in the path
        3. Number of malfunctioning agents (going in my direction) identified in the subpath 
           from the root up to each node in the path
        4. Number of malfunctioning agents (going in a direction different from mine) identified 
           in the subpath from the root up to each node in the path
        5. Minimum distances from an agent to other agent's (going in my direction) 
           in each edge of the path
        6. Minimum distances from an agent to other agent's (going in a direction 
           different than mine) in each edge of the path
        7. Maximum number of malfunctioning turns of other agents (going in my direction),
           in each edge of the path
        8. Maximum number of malfunctioning turns of other agents (going in a direction 
           different from mine), in each edge of the path
        9. Distances from the target, from each node in the path
        10. Number of agents using the node to reach their target in the shortest path
        11. Number of agents in deadlock in the previous path, assuming that all the 
            other agents follow their shortest path
        12. How many turns before a possible deadlock
'''

'''
Todo:
    - Observation:
        * Test deadlock distance with variable speeds
        * Test malfunctions observations
        * Add malfunction turns to cumulative weights
        * Check normalization correctness
        * Handle observation not present at all
        * Store agents in deadlock and substitute their shortest paths 
          as if they cannot reach their target (i.e. store current and next node)
        * Add function to check for current deadlocks (agents one in front of another or cycles)
        * The first agents to enter the rail are the fastest ones (not the ones with minimum handles)
'''

# SpeedData:
# - `times` represents the total number of turns required for an agent to complete a cell
# - `remaining` represents the remaining number of steps required for an agent to complete the current cell
SpeedData = namedtuple('SpeedData', ['times', 'remaining'])


class CustomObservation(ObservationBuilder):

    FEATURES = 12
    LOWER, UPPER = -1, 1
    UNDER, OVER = -2, 2

    def __init__(self, max_depth, predictor):
        super().__init__()
        self.max_depth = max_depth
        self.predictor = predictor
        self.observations = dict()

    def _init_env(self):
        '''
        Initialize the railway environment and the
        predictor object
        '''
        self.railway_encoding = CellOrientationGraph(
            grid=self.env.rail.grid, agents=self.env.agents
        )
        if self.predictor is not None:
            self.predictor.set_railway_encoding(self.railway_encoding)
            self.predictor.reset()

    def _init_agents(self):
        '''
        Store agent-related info:
        - `speed_data`: a SpeedData object for each agent
        - `agent_handles`: set of agent handles
        - `other_agents`: list of other agent's handles for each agent
        - `last_nodes`: list of last visited nodes for each agent
          (along with corresponding weights)
        '''
        self.agent_handles = set(self.env.get_agent_handles())
        self.other_agents = dict()
        self.speed_data = dict()
        self.last_nodes = []
        self.last_positions = []
        for handle, agent in enumerate(self.env.agents):
            times_per_cell = int(np.reciprocal(agent.speed_data["speed"]))
            self.speed_data[handle] = SpeedData(
                times=times_per_cell, remaining=0
            )
            self.other_agents[handle] = self.agent_handles - {handle}
            agent_position = self.railway_encoding.get_agent_cell(handle)
            prev_node, prev_weight = self.railway_encoding.previous_node(
                agent_position
            )
            self.last_nodes.append(
                (prev_node, prev_weight * times_per_cell)
            )
            self.last_positions.append(agent_position)

    def reset(self):
        self._init_env()
        self._init_agents()

    def set_env(self, env):
        super().set_env(env)
        if self.predictor:
            self.predictor.set_env(self.env)

    def _update_shortest(self, handle, prediction):
        '''
        Store shortest paths, shortest positions and shortest cumulative weights
        for the current observation of the given agent
        '''
        # Update speed data
        remaining_turns_in_cell = 0
        if self.env.agents[handle].speed_data["speed"] < 1.0:
            remaining_turns_in_cell = int(
                (1 - np.clip(self.env.agents[handle].speed_data["position_fraction"], 0.0, 1.0)) /
                self.env.agents[handle].speed_data["speed"]
            )
        self.speed_data[handle] = SpeedData(
            times=self.speed_data[handle].times,
            remaining=remaining_turns_in_cell
        )

        # Update shortest paths
        shortest_path = np.array(prediction.path, np.dtype('int, int, int'))
        self._shortest_paths[handle, :shortest_path.shape[0]] = shortest_path

        # Update shortest positions
        shortest_positions = np.array(
            [node[:-1] for node in prediction.path], np.dtype('int, int')
        )
        self._shortest_positions[handle, :shortest_positions.shape[0]] = (
            shortest_positions
        )

        # Update shortest cumulative weights
        shortest_cum_weights = np.array(
            self.compute_cumulative_weights(
                handle, prediction.edges, remaining_turns_in_cell
            )
        )
        self._shortest_cum_weights[handle, :shortest_cum_weights.shape[0]] = (
            shortest_cum_weights
        )

        # Update last visited node and last positions
        agent_position = tuple(shortest_path[0])
        if self.last_positions[handle] != agent_position:
            if agent_position in self.railway_encoding.graph.nodes:
                self.last_nodes[handle] = (agent_position, 0)
            else:
                self.last_nodes[handle] = (
                    self.last_nodes[handle][0],
                    self.last_nodes[handle][1] + 1
                )
            self.last_positions[handle] = agent_position

    def get_many(self, handles=None):
        self.predictions = self.predictor.get_many()
        self._shortest_paths = np.full(
            (len(self.agent_handles), self.max_depth),
            -1, np.dtype('int, int, int')
        )
        self._shortest_positions = np.full(
            (len(self.agent_handles), self.max_depth), -1, np.dtype('int, int')
        )
        self._shortest_cum_weights = np.zeros(
            (len(self.agent_handles), self.max_depth)
        )
        for handle, prediction in self.predictions.items():
            # Check if agent is not at target
            if self.predictions[handle] is not None:
                shortest_prediction = prediction[0]
                self._update_shortest(handle, shortest_prediction)

        return super().get_many(handles)

    def get(self, handle=0):
        self.observations[handle] = np.full(
            (self.max_depth, self.max_depth, self.FEATURES), -np.inf
        )
        if self.predictions[handle] is not None:
            shortest_path_prediction, deviation_paths_prediction = self.predictions[handle]
            packed_positions, packed_weights = self._get_shortest_packed_positions()
            shortest_feats = self._fill_path_values(
                handle, shortest_path_prediction, packed_positions, packed_weights
            )
            prev_num_agents = shortest_feats[:, :4]
            self.observations[handle][0, :, :] = shortest_feats
            for i, deviation_prediction in enumerate(deviation_paths_prediction):
                prev_deadlocks = 0
                prev_num_agents_values = None
                if i >= 1:
                    prev_deadlocks = shortest_feats[i - 1, 10]
                    prev_num_agents_values = prev_num_agents[i - 1, :]
                dev_feats = self._fill_path_values(
                    handle, deviation_prediction, packed_positions, packed_weights,
                    turns_to_deviation=self._shortest_cum_weights[handle, i],
                    prev_deadlocks=prev_deadlocks, prev_num_agents=prev_num_agents_values
                )
                self.observations[handle][i + 1, :, :] = dev_feats

        '''
        print()
        print(f'PREHandle: {handle}')
        print(self.observations[handle])
        print()
        '''
        self.observations[handle] = self.normalize(self.observations[handle])
        '''
        print()
        print(f'POSTHandle: {handle}')
        print(self.observations[handle])
        print()
        '''
        return self.observations[handle]

    def _fill_path_values(self, handle, prediction, packed_positions, packed_weights,
                          turns_to_deviation=0, prev_deadlocks=0, prev_num_agents=None):
        '''
        Compute observations for the given prediction and return
        a suitable feature matrix
        '''
        # Adjust weights and positions based on which kind of path
        # we are analyzing (shortest or deviation)
        path_weights = np.zeros((self.max_depth,))
        path = prediction.path
        positions = [node[:-1] for node in path]
        if turns_to_deviation == 0:
            weights = self._shortest_cum_weights[handle]
            positions = packed_positions[handle].tolist()[:len(path)]
            positions_weights = packed_weights[handle]
            path_weights[:weights.shape[0]] = weights
        else:
            weights = np.array(
                self.compute_cumulative_weights(
                    handle, prediction.edges, turns_to_deviation
                )
            )
            path_weights[:weights.shape[0]] = weights
            positions_weights = path_weights

        # Compute features
        num_agents, agent_distances, malfunctions = self.agents_in_path(
            handle, path, path_weights, prev_num_agents=prev_num_agents
        )
        target_distances = self.distance_from_target(
            handle, prediction.lenght, path, path_weights, turns_to_deviation
        )
        c_nodes = self.common_nodes(handle, positions)
        deadlocks, deadlock_distances = self.find_deadlocks(
            handle, positions, positions_weights, packed_positions, packed_weights,
            prev_deadlocks=prev_deadlocks
        )

        # Build the feature matrix
        feature_matrix = np.vstack([
            num_agents, agent_distances, malfunctions,
            target_distances, c_nodes, deadlocks, deadlock_distances
        ]).T

        return feature_matrix

    def compute_cumulative_weights(self, handle, edges, initial_distance):
        '''
        Given a list of edges, compute the cumulative sum of weights,
        representing the number of turns the given agent must perform
        to reach each node in the path
        '''
        weights = [initial_distance] + [
            e[2]['weight'] * self.speed_data[handle].times for e in edges
        ]
        return np.cumsum(weights)

    def agents_in_path(self, handle, path, cum_weights, prev_num_agents=None):
        '''
        Return three arrays:
        - Number of agents identified in the subpath from the root up to
          each node in the path (in both directions and both malfunctioning or not)
        - Minimum distances from an agent to other agent's
          in each edge of the path (in both directions)
        - Maximum turns that an agent has to wait because it is malfunctioning,
          in each edge of the path (in both directions)

        The directions are considered as:
        - Same direction, if two agents "follow" each other
        - Other direction, otherwise
        '''
        num_agents = np.zeros((self.max_depth, 4))
        if prev_num_agents is not None:
            num_agents[:] = np.array(prev_num_agents)
        distances = np.full((self.max_depth, 2), np.inf)
        malfunctions = np.zeros((self.max_depth, 2))

        # For each agent different than myself
        for agent in self.other_agents[handle]:
            position = self.railway_encoding.get_agent_cell(agent)
            # Check if agent is not DONE_REMOVED (position would be None)
            if position is not None:
                # Take the other agent's next node in the packed graph
                node, next_node_distance = self.railway_encoding.next_node(
                    position
                )
                # Take every possible direction for the given node in the packed graph
                nodes = self.railway_encoding.get_nodes((node[0], node[1]))
                # Check the next nodes of the next node in order to see
                # the other agent's entry direction
                next_nodes = list(self.railway_encoding.graph.successors(node))

                # Check if one of the next nodes of the other agent are in my path
                for other_node in nodes:
                    index = utils.get_index(path, other_node)
                    if index is not None:
                        # Initialize distances
                        distance = cum_weights[index]
                        if cum_weights[index] < self.speed_data[handle].times:
                            distance = (
                                self.speed_data[handle].remaining -
                                self.speed_data[handle].times
                            )
                        turns_to_reach_other_agent = abs(
                            (next_node_distance - (self.speed_data[agent].remaining / self.speed_data[agent].times)) *
                            self.speed_data[handle].times
                        )

                        # Check if same direction or other direction
                        different_node = other_node != node
                        more_than_one_choice = len(next_nodes) > 1
                        last_node_in_path = len(path) <= index + 1
                        different_one_choice = (
                            not last_node_in_path and
                            len(next_nodes) > 0 and
                            next_nodes[0] != path[index + 1]
                        )
                        if (different_node and (more_than_one_choice or last_node_in_path or different_one_choice)):
                            direction = 1
                        else:
                            turns_to_reach_other_agent = -turns_to_reach_other_agent
                            direction = 0

                        # Update number of agents
                        num_agents[index:len(path), direction] += 1

                        # Update distances s.t. we always keep the greatest one (if distance is negative),
                        # otherwise the minimum one (if distance is positive)
                        distance += turns_to_reach_other_agent
                        if ((distances[index, direction] == np.inf) or
                            (distance >= 0 and distances[index, direction] > distance) or
                                (distance <= 0 and distances[index, direction] < distance) or
                                (distance >= 0 and distances[index, direction] < 0)):
                            distances[index, direction] = distance

                        # Update malfunctions
                        malfunction = self.env.agents[agent].malfunction_data['malfunction']
                        if malfunction > 0:
                            num_agents[index:len(path), direction + 2] += 1
                        if malfunctions[index, direction] < malfunction:
                            malfunctions[index, direction] = malfunction
                        break

        return np.transpose(num_agents), np.transpose(distances), np.transpose(malfunctions)

    def distance_from_target(self, handle, lenght, path, cum_weights, turns_to_deviation=0):
        '''
        For a shortest path:
        - `lenght` should be the actual length of the shortest path
        - `cum_weights` should be the cumulative number of turns to reach each node
        - `turns_to_deviation` should be zero

        For a deviation path:
        - `lenght` should be the actual length of the deviation path
        - `cum_weights` should be the cumulative number of turns to reach each node
          (starting from the agent's position instead of the root of the deviation path)
        - `turns_to_deviation` should be the number of turns required to reach the root
           of the deviation path

        Returns the actual distance from each node of the path to its target
        '''
        # If the agent cannot arrive to the target
        if lenght == np.inf:
            return np.full((self.max_depth,), np.inf)

        # Initialize each node with the distance from the agent to the target
        distances = np.zeros((self.max_depth,))
        max_distance = (
            (lenght * self.speed_data[handle].times)
            + turns_to_deviation
        )
        distances[:len(path)] = np.full((len(path),), max_distance)

        # Compute actual distances for each node
        distances -= cum_weights
        return distances

    def common_nodes(self, handle, positions):
        '''
        Given an agent's positions and the shortest positions for every other agent,
        compute the number of agents intersecting at each node
        '''
        c_nodes = np.zeros((self.max_depth,))
        if len(positions) > 0:
            nd_positions = np.array(positions, np.dtype('int, int'))
            computed = np.zeros((len(positions),))
            for row in self.other_agents[handle]:
                computed += np.count_nonzero(
                    np.isin(
                        nd_positions, self._shortest_positions[row, :]
                    ).reshape(1, len(nd_positions)),
                    axis=0
                )
            c_nodes[:computed.shape[0]] = computed
        return c_nodes

    def _get_shortest_packed_positions(self):
        '''
        For each agent's shortest path, substitute the first node for
        its previous node in the packed graph, if it doesn't
        already match with the agent's position

        Return the modified path (without the direction component),
        along with the associated cumulative weights (which are re-computed
        starting from the original cumulative weights)
        '''
        prev_weights = []
        prev_nodes = [node[0] for node in self.last_nodes]
        for agent, path in enumerate(self._shortest_paths):
            # If the agent's position is not on the packed graph
            if tuple(path[0]) != prev_nodes[agent]:
                prev_weights.append(
                    - (self.last_nodes[agent][1] +
                       self.speed_data[agent].times -
                       self.speed_data[agent].remaining)
                )
            # If the agent's position is already in the packed path,
            # do not change the cumulative weights of the first node
            else:
                prev_weights.append(self._shortest_cum_weights[agent, 0])

        # Remove the first column of the original shortest positions
        # and replace it with the previous node
        packed_positions = np.hstack([
            np.array(
                [node[:-1] for node in prev_nodes], np.dtype('int, int')
            ).reshape(self._shortest_positions.shape[0], 1),
            self._shortest_positions[:, 1:]
        ])
        # Update the corresponding cumulative weights
        packed_weights = np.hstack([
            np.array(prev_weights).reshape(
                self._shortest_cum_weights.shape[0], 1
            ),
            self._shortest_cum_weights[:, 1:]
        ])
        return packed_positions, packed_weights

    def find_deadlocks(self, handle, positions, cum_weights, packed_positions, packed_weights, prev_deadlocks=0):
        '''
        For a shortest path and a deviation path:
        - `positions` should be the packed positions
        - `cum_weights` should be the packed cumulative weights
        - `packed_positions` should be the list of packed shortest positions for each agent
        - `packed_weights` should be the list of packed cumulative weights for each agent

        Returns two lists:
        - `deadlocks`: the number of possible deadlocks for each node in `path`
        - `crash_turns`: the number of turns to the first deadlock for each node in `path`
        '''
        deadlocks = np.full((self.max_depth,), prev_deadlocks)
        crash_turns = np.full((self.max_depth,), np.inf)

        # For each agent different than myself
        for agent in self.other_agents[handle]:
            deadlock_found = False
            agent_path = packed_positions[agent].tolist()
            # For each node in the other agent's path
            for i in range(len(agent_path) - 1):
                # Avoid non-informative pair of nodes
                if tuple(agent_path[i]) != (-1, -1) and tuple(agent_path[i + 1]) != (-1, -1):
                    # For each node in my path
                    for j in range(len(positions) - 1):
                        source, dest = positions[j], positions[j + 1]
                        from_dest_to_source = (
                            source == agent_path[i + 1] and
                            dest == agent_path[i]
                        )
                        intersecting_turns = (
                            not cum_weights[j] > packed_weights[agent, i + 1] and
                            not cum_weights[j + 1] < packed_weights[agent, i]
                        )
                        deadlock_found = from_dest_to_source and intersecting_turns
                        if deadlock_found:
                            space = (
                                cum_weights[j + 1] - cum_weights[j]
                            ) / self.speed_data[handle].times

                            '''
                            print()
                            print(
                                f'Handle {handle} -> Deadlock Edge: {source}-{dest} Space: {space} \n My Turns {cum_weights[j],cum_weights[j+1]} \n Oth Turns {packed_weights[agent, i],packed_weights[agent, i+1]}')
                            print()
                            '''

                            # Both agents in same edge: reduce space by how much they
                            # already have traversed
                            if cum_weights[j] < 0 and packed_weights[agent, i] < 0:
                                space += (
                                    cum_weights[j] /
                                    self.speed_data[handle].times
                                )
                                space += (
                                    packed_weights[agent, i] /
                                    self.speed_data[agent].times
                                )
                            # My entry turn is greater than the other agent's entry turn:
                            # reduce space by how the other agent's has already traversed,
                            # by the time my agent enters the edge
                            elif cum_weights[j] > packed_weights[agent, i]:
                                space -= abs(
                                    cum_weights[j] -
                                    abs(packed_weights[agent, i])
                                ) / self.speed_data[agent].times
                            # The opposite of the previous case
                            elif packed_weights[agent, i] > cum_weights[j]:
                                space += abs(
                                    packed_weights[agent, i] -
                                    abs(cum_weights[j])
                                ) / self.speed_data[agent].times

                            # Compute the distance in turns from my agent to
                            # the possible identified deadlock
                            crash_turn = np.ceil(
                                np.clip(cum_weights[j], 0, None) +
                                space / utils.reciprocal_sum(
                                    self.speed_data[agent].times,
                                    self.speed_data[handle].times
                                )
                            )
                            # Store only the minimum distance
                            if crash_turns[j] > crash_turn:
                                crash_turns[j] = crash_turn

                            # Update number of deadlocks
                            deadlocks[j:len(positions)] += 1

                            # If one deadlock is found, do not check any other
                            # between the same pair of agents
                            break

                    # If one deadlock is found, do not check any other
                    # between the same pair of agents
                    if deadlock_found:
                        break

        return deadlocks, crash_turns

    def normalize(self, observation):
        '''
        Normalize the given observations by performing min-max scaling
        over individual features
        '''
        normalized_observation = observation.copy()
        num_agents = normalized_observation[:, :, 0:4]
        agent_distances = normalized_observation[:, :, 4:6]
        malfunctions = normalized_observation[:, :, 6:8]
        target_distances = normalized_observation[:, :, 8]
        c_nodes = normalized_observation[:, :, 9]
        deadlocks = normalized_observation[:, :, 10]
        deadlock_distances = normalized_observation[:, :, 11]

        # Normalize number of agents in path
        done_agents = sum([
            1 for i in self.agent_handles
            if self.env.agents[i].status in (RailAgentStatus.DONE, RailAgentStatus.DONE_REMOVED)
        ])
        remaining_agents = len(self.agent_handles) - done_agents
        num_agents = utils.min_max_scaling(
            num_agents, self.LOWER, self.UPPER, self.UNDER, self.OVER,
            known_min=0, known_max=remaining_agents
        )

        # Normalize malfunctions
        max_malfunctions = self.env.malfunction_generator.get_process_data().max_duration
        malfunctions = utils.min_max_scaling(
            malfunctions, self.LOWER, self.UPPER, self.UNDER, self.OVER,
            known_min=0, known_max=max_malfunctions
        )

        # Normalize common nodes
        c_nodes = utils.min_max_scaling(
            c_nodes, self.LOWER, self.UPPER, self.UNDER, self.OVER,
            known_min=0, known_max=self.max_depth
        )

        # Normalize deadlocks
        deadlocks = utils.min_max_scaling(
            deadlocks, self.LOWER, self.UPPER, self.UNDER, self.OVER,
            known_min=0, known_max=remaining_agents
        )

        # Normalize distances
        agent_distances = utils.min_max_scaling(
            agent_distances, self.LOWER, self.UPPER, self.UNDER, self.OVER
        )
        target_distances = utils.min_max_scaling(
            target_distances, self.LOWER, self.UPPER, self.UNDER, self.OVER
        )
        deadlock_distances = utils.min_max_scaling(
            deadlock_distances, self.LOWER, self.UPPER, self.UNDER, self.OVER
        )

        # Build the normalized observation
        normalized_observation[:, :, 0:4] = num_agents
        normalized_observation[:, :, 4:6] = agent_distances
        normalized_observation[:, :, 6:8] = malfunctions
        normalized_observation[:, :, 8] = target_distances
        normalized_observation[:, :, 9] = c_nodes
        normalized_observation[:, :, 10] = deadlocks
        normalized_observation[:, :, 11] = deadlock_distances

        # Sanity check
        normalized_observation[normalized_observation == -np.inf] = self.UNDER
        normalized_observation[normalized_observation == np.inf] = self.OVER

        # Check if the output is in range [UNDER, OVER]
        assert np.logical_and(
            normalized_observation >= self.UNDER,
            normalized_observation <= self.OVER
        ).all()

        return normalized_observation
