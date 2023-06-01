import numpy as np
import pandas as pd

import torch
import torch.nn as nn
# import torch.nn.functional as F

from datetime import datetime
import itertools
import argparse
# import re
import os
import pickle

from sklearn.preprocessing import StandardScaler


def get_data():
    df = pd.read_csv('data.csv')
    return df.values


# The experience replay memory #
class ReplayBuffer:
    def __init__(self, obs_dim, act_dim, size):
        self.obs1_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.obs2_buf = np.zeros([size, obs_dim], dtype=np.float32)
        self.acts_buf = np.zeros(size, dtype=np.uint8)
        self.rews_buf = np.zeros(size, dtype=np.float32)
        self.done_buf = np.zeros(size, dtype=np.uint8)
        self.ptr, self.size, self.max_size = 0, 0, size

    def store(self, obs, act, rew, next_obs, done):
        self.obs1_buf[self.ptr] = obs
        self.obs2_buf[self.ptr] = next_obs
        self.acts_buf[self.ptr] = act
        self.rews_buf[self.ptr] = rew
        self.done_buf[self.ptr] = done
        self.ptr = (self.ptr+1) % self.max_size
        self.size = min(self.size+1, self.max_size)

    def sample_batch(self, batch_size=32):
        idxs = np.random.randint(0, self.size, size=batch_size)
        return dict(s=self.obs1_buf[idxs],
                    s2=self.obs2_buf[idxs],
                    a=self.acts_buf[idxs],
                    r=self.rews_buf[idxs],
                    d=self.done_buf[idxs])


def get_scaler(env):
    # return scikit-learn scaler object to scale the states
    # Note: you could also populate the replay buffer here

    states = []
    for _ in range(env.n_step):
        action = np.random.choice(env.action_space)
        state, reward, done, info = env.step(action)
        states.append(state)
        if done:
            break

    scaler = StandardScaler()
    scaler.fit(states)
    return scaler


def maybe_make_dir(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)


class MLP(nn.Module):
    def __init__(self, n_inputs, n_action, n_hidden_layers=1, hidden_dim=32):
        super(MLP, self).__init__()

        M = n_inputs
        self.layers = []
        for _ in range(n_hidden_layers):
            layer = nn.Linear(M, hidden_dim)
            M = hidden_dim
            self.layers.append(layer)
            self.layers.append(nn.ReLU())

        # final layer
        self.layers.append(nn.Linear(M, n_action))
        self.layers = nn.Sequential(*self.layers)

    def forward(self, X):
        return self.layers(X)

    def save_weights(self, path):
        torch.save(self.state_dict(), path)

    def load_weights(self, path):
        self.load_state_dict(torch.load(path))


def predict(model, np_states):
    with torch.no_grad():
        inputs = torch.from_numpy(np_states.astype(np.float32))
        output = model(inputs)
        # print("output:", output)
        return output.numpy()


def train_one_step(model, criterion, optimizer, inputs, targets):
    # convert to tensors
    inputs = torch.from_numpy(inputs.astype(np.float32))
    targets = torch.from_numpy(targets.astype(np.float32))

    # zero the parameter gradients
    optimizer.zero_grad()

    # Forward pass
    outputs = model(inputs)
    loss = criterion(outputs, targets)

    # Backward and optimize
    loss.backward()
    optimizer.step()


class MultiGridEnv:
    """
    A 3-grid dispatching environment.
    State: vector of size 7 (n_grid * 2 + 1)
      - # dispatches of grid 1
      - # dispatches of grid 2
      - # dispatches of grid 3
      - demand of grid 1 (using daily close demand)
      - demand of grid 2
      - demand of grid 3
      - vehicles(or cash) owned (good investment , more vehicles)
    Action: categorical variable with 27 (3^3) possibilities
      - for each grid, you can:
      - 0 = remove
      - 1 = hold
      - 2 = add
    """

    def __init__(self, data, initial_investment=20000):
        # data
        self.grid_demand_history = data
        self.n_step, self.n_grid = self.grid_demand_history.shape

        # instance attributes
        self.initial_investment = initial_investment
        self.cur_step = None
        self.grid_owned = None
        self.grid_demand = None
        self.vehicle_in_hand = None

        self.action_space = np.arange(3**self.n_grid)

        # action permutations
        # returns a nested list with elements like:
        # [0,0,0]
        # [0,0,1]
        # [0,0,2]
        # [0,1,0]
        # [0,1,1]
        # etc.
        # 0 = sell
        # 1 = hold
        # 2 = buy
        self.action_list = list(
            map(list, itertools.product([0, 1, 2], repeat=self.n_grid)))

        # calculate size of state
        self.state_dim = self.n_grid * 2 + 1

        self.reset()

    def reset(self):
        self.cur_step = 0
        self.grid_owned = np.zeros(self.n_grid)
        self.grid_demand = self.grid_demand_history[self.cur_step]
        self.vehicle_in_hand = self.initial_investment
        return self._get_obs()

    def step(self, action):
        assert action in self.action_space

        # get current value before performing the action
        prev_val = self._get_val()

        # update demand, i.e. go to the next day
        self.cur_step += 1
        self.grid_demand = self.grid_demand_history[self.cur_step]

        # perform the trade
        self._trade(action)

        # get the new value after taking the action
        cur_val = self._get_val()

        # reward is the increase in porfolio value
        reward = cur_val - prev_val

        # done if we have run out of data
        done = self.cur_step == self.n_step - 1

        # store the current value of the portfolio here
        info = {'cur_val': cur_val}

        # conform to the Gym API
        return self._get_obs(), reward, done, info

    def _get_obs(self):
        obs = np.empty(self.state_dim)
        obs[:self.n_grid] = self.grid_owned
        obs[self.n_grid:2*self.n_grid] = self.grid_demand
        obs[-1] = self.vehicle_in_hand
        return obs

    def _get_val(self):
        return self.grid_owned.dot(self.grid_demand) + self.vehicle_in_hand

    def _trade(self, action):
        # index the action we want to perform
        # 0 = sell
        # 1 = hold
        # 2 = buy
        # e.g. [2,1,0] means:
        # buy first grid
        # hold second grid
        # sell third grid
        action_vec = self.action_list[action]

        # determine which grids to buy or sell
        sell_index = []  # stores index of grids we want to sell
        buy_index = []  # stores index of grids we want to buy
        for i, a in enumerate(action_vec):
            if a == 0:
                sell_index.append(i)
            elif a == 2:
                buy_index.append(i)

        # sell any grids we want to sell
        # then buy any grids we want to buy
        if sell_index:
            # NOTE: to simplify the problem, when we sell, we will sell ALL grids of that grid
            for i in sell_index:
                self.vehicle_in_hand += self.grid_demand[i] * \
                    self.grid_owned[i]
                self.grid_owned[i] = 0
        if buy_index:
            # NOTE: when buying, we will loop through each grid we want to buy,
            #       and buy one grid at a time until we run out of vehicle
            can_buy = True
            while can_buy:
                for i in buy_index:
                    if self.vehicle_in_hand > self.grid_demand[i]:
                        self.grid_owned[i] += 1  # buy one grid
                        self.vehicle_in_hand -= self.grid_demand[i]
                    else:
                        can_buy = False


class DQNAgent(object):
    def __init__(self, state_size, action_size):
        self.state_size = state_size
        self.action_size = action_size
        self.memory = ReplayBuffer(state_size, action_size, size=500)
        self.gamma = 0.95  # discount rate
        self.epsilon = 1.0  # exploration rate
        self.epsilon_min = 0.01
        self.epsilon_decay = 0.995
        self.model = MLP(state_size, action_size)

        # Loss and optimizer
        self.criterion = nn.MSELoss()
        self.optimizer = torch.optim.Adam(self.model.parameters())

    def update_replay_memory(self, state, action, reward, next_state, done):
        self.memory.store(state, action, reward, next_state, done)

    def act(self, state):
        if np.random.rand() <= self.epsilon:
            return np.random.choice(self.action_size)
        act_values = predict(self.model, state)
        return np.argmax(act_values[0])  # returns action

    def replay(self, batch_size=32):
        # first check if replay buffer contains enough data
        if self.memory.size < batch_size:
            return

        # sample a batch of data from the replay memory
        minibatch = self.memory.sample_batch(batch_size)
        states = minibatch['s']
        actions = minibatch['a']
        rewards = minibatch['r']
        next_states = minibatch['s2']
        done = minibatch['d']

        # Calculate the target: Q(s',a)
        target = rewards + (1 - done) * self.gamma * \
            np.amax(predict(self.model, next_states), axis=1)

        # With the PyTorch API, it is simplest to have the target be the
        # same shape as the predictions.
        # However, we only need to update the network for the actions
        # which were actually taken.
        # We can accomplish this by setting the target to be equal to
        # the prediction for all values.
        # Then, only change the targets for the actions taken.
        # Q(s,a)
        target_full = predict(self.model, states)
        target_full[np.arange(batch_size), actions] = target

        # Run one training step
        train_one_step(self.model, self.criterion,
                       self.optimizer, states, target_full)

        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay

    def load(self, name):
        self.model.load_weights(name)

    def save(self, name):
        self.model.save_weights(name)


def play_one_episode(agent, env, is_train):
    # note: after transforming states are already 1xD
    state = env.reset()
    state = scaler.transform([state])
    done = False

    while not done:
        action = agent.act(state)
        next_state, reward, done, info = env.step(action)
        next_state = scaler.transform([next_state])
        if is_train == 'train':
            agent.update_replay_memory(state, action, reward, next_state, done)
            agent.replay(batch_size)
        state = next_state

    return info['cur_val']


if __name__ == '__main__':

    # config
    models_folder = 'rl_rebalancer_models'
    rewards_folder = 'rl_rebalancer_rewards'
    num_episodes = 2000
    batch_size = 32
    initial_investment = 20000

    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--mode', type=str, required=True,
                        help='either "train" or "test"')
    args = parser.parse_args()

    maybe_make_dir(models_folder)
    maybe_make_dir(rewards_folder)

    data = get_data()
    n_timesteps, n_grids = data.shape

    n_train = n_timesteps // 2

    train_data = data[:n_train]
    test_data = data[n_train:]

    env = MultiGridEnv(train_data, initial_investment)
    state_size = env.state_dim
    action_size = len(env.action_space)
    agent = DQNAgent(state_size, action_size)
    scaler = get_scaler(env)

    # store the final value of the portfolio (end of episode)
    portfolio_value = []

    if args.mode == 'test':
        # then load the previous scaler
        with open(f'{models_folder}/scaler.pkl', 'rb') as f:
            scaler = pickle.load(f)

        # remake the env with test data
        env = MultiGridEnv(test_data, initial_investment)

        # make sure epsilon is not 1!
        # no need to run multiple episodes if epsilon = 0, it's deterministic
        agent.epsilon = 0.01

        # load trained weights
        agent.load(f'{models_folder}/dqn.ckpt')

    # play the game num_episodes times
    for e in range(num_episodes):
        t0 = datetime.now()
        val = play_one_episode(agent, env, args.mode)
        dt = datetime.now() - t0
        print(
            f"episode: {e + 1}/{num_episodes}, episode end value: {val:.2f}, duration: {dt}")
        portfolio_value.append(val)  # append episode end portfolio value

    # save the weights when we are done
    if args.mode == 'train':
        # save the DQN
        agent.save(f'{models_folder}/dqn.ckpt')

        # save the scaler
        with open(f'{models_folder}/scaler.pkl', 'wb') as f:
            pickle.dump(scaler, f)

    # save portfolio value for each episode
    np.save(f'{rewards_folder}/{args.mode}.npy', portfolio_value)
