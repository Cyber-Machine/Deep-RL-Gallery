import time

import gym
import ray
import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt

from model import RecurrentQNetwork
from buffer import EpisodeBuffer, SegmentReplayBuffer


@ray.remote
class Actor:

    def __init__(self, pid, env_name,
                 epsilon, gamma, eta, alpha,
                 burnin_length, unroll_length):

        self.pid = pid
        self.env_name = env_name
        self.action_space = gym.make(env_name).action_space.n

        self.q_network = RecurrentQNetwork(self.action_space)
        self.epsilon = epsilon
        self.gamma = gamma

        self.eta = eta
        self.alpha = alpha  # priority exponent

        self.burnin_len = burnin_length
        self.unroll_len = unroll_length

        self.define_network()

    def define_network(self):
        tf.config.set_visible_devices([], 'GPU')
        env = gym.make(self.env_name)
        state = env.reset()

        c, h = self.q_network.lstm.get_initial_state(batch_size=1, dtype=tf.float32)
        self.q_network(np.atleast_2d(state), states=[c, h])

    def sync_weights_and_rollout(self, current_weights):

        #: グローバルQ-Networkと重みを同期
        self.q_network.set_weights(current_weights)

        priorities, segments = [], []

        while len(segments) < 10:
            _priorities, _segments = self._rollout()
            priorities += _priorities
            segments += _segments

        return priorities, segments, self.pid

    def _rollout(self) -> (list, list):

        env = gym.make(self.env_name)
        episode_buffer = EpisodeBuffer(burnin_length=self.burnin_len,
                                       unroll_length=self.unroll_len)

        state = env.reset().astype(np.float32)
        c, h = self.q_network.lstm.get_initial_state(
            batch_size=1, dtype=tf.float32)
        done = False
        episode_rewards = 0
        while not done:
            action, (next_c, next_h) = self.q_network.sample_action(state, c, h, self.epsilon)
            next_state, reward, done, _ = env.step(action)
            episode_rewards += reward

            transition = (state, action, reward, next_state, done, c, h)
            episode_buffer.add(transition)

            state, c, h = next_state, next_c, next_h

        #print(episode_rewards)

        segments = episode_buffer.pull()

        """ Compute initial priority
        """
        states = np.stack([seg.states for seg in segments], axis=1)    # (burnin_len+unroll_len, batch_size, obs_dim)
        actions = np.stack([seg.actions for seg in segments], axis=1)  # (unroll_len, batch_size)
        rewards = np.stack([seg.rewards for seg in segments], axis=1)  # (unroll_len, batch_size)
        dones = np.stack([seg.dones for seg in segments], axis=1)      # (unroll_len, batch_size)
        last_state = np.stack([seg.last_state for seg in segments])    # (batch_size, obs_dim)

        c = tf.convert_to_tensor(
            np.vstack([seg.c_init for seg in segments]), dtype=tf.float32)  # (batch_size, lstm_out_dim)
        h = tf.convert_to_tensor(
            np.vstack([seg.h_init for seg in segments]), dtype=tf.float32)  # (batch_size, lstm_out_dim)

        #: burn-in with stored-state
        for t in range(self.burnin_len):
            _, (c, h) = self.q_network(states[t], states=[c, h])

        qvalues = []
        for t in range(self.burnin_len, self.burnin_len+self.unroll_len):
            q, (c, h) = self.q_network(states[t], states=[c, h])
            qvalues.append(q)
        qvalues = tf.stack(qvalues)                                          # (unroll_len, batch_size, action_space)
        actions_onehot = tf.one_hot(actions, self.action_space)
        Q = tf.reduce_sum(qvalues * actions_onehot, axis=2, keepdims=False)  # (unroll_len, batch_size)

        #: compute qvlalue of last next-state in segment
        remaining_qvalue, _ = self.q_network(last_state, states=[c, h])
        remaining_qvalue = tf.expand_dims(remaining_qvalue, axis=0)          # (1, batch_size, action_space)

        next_qvalues = tf.concat([qvalues[1:], remaining_qvalue], axis=0)    # (unroll_len, batch_size, action_space)
        next_actions = tf.argmax(next_qvalues, axis=2)                       # (unroll_len, batch_size)
        next_actions_onehot = tf.one_hot(next_actions, self.action_space)    # (unroll_len, batch_size, action_space)
        next_maxQ = tf.reduce_sum(
            next_qvalues * next_actions_onehot, axis=2, keepdims=False)      # (unroll_len, batch_size)

        TQ = rewards + self.gamma * (1 - dones) * next_maxQ  # (unroll_len, batch_size)

        td_errors = TQ - Q
        td_errors_abs = tf.abs(td_errors)

        priorities = self.eta * tf.reduce_max(td_errors_abs, axis=0) \
            + (1 - self.eta) * tf.reduce_mean(td_errors_abs, axis=0)
        priorities = (priorities + 0.001) ** self.alpha

        return priorities.numpy().tolist(), segments


@ray.remote(num_cpus=1, num_gpus=1)
class Learner:

    def __init__(self, env_name, gamma, eta, alpha,
                 burnin_length, unroll_length):
        self.env_name = env_name
        self.action_space = gym.make(self.env_name).action_space.n

        self.q_network = RecurrentQNetwork(self.action_space)
        self.target_q_network = RecurrentQNetwork(self.action_space)
        self.optimizer = tf.keras.optimizers.Adam(lr=0.001)

        self.gamma = gamma
        self.eta = eta
        self.alpha = alpha

        self.burnin_len = burnin_length
        self.unroll_len = unroll_length

    def define_network(self):
        env = gym.make(self.env_name)
        state = env.reset()
        c, h = self.q_network.lstm.get_initial_state(batch_size=1, dtype=tf.float32)

        self.q_network(np.atleast_2d(state), states=[c, h])
        self.target_q_network(np.atleast_2d(state), states=[c, h])
        self.target_q_network.set_weights(self.q_network.get_weights())
        current_weights = self.q_network.get_weights()
        return current_weights

    def update_network(self, minibatchs):
        """
        Args:
            minibatchs (List[Tuple(indices, weights, segments)])
              indices (List[float]): indices of replay buffer
              weights (List[float]): Importance sampling weights
              segments (List[Segment]):
                Segment: sequence of transitions
        """

        indices_all = []
        priorities_all = []

        for (indices, weights, segments) in minibatchs:

            states = np.stack([seg.states for seg in segments], axis=1)    # (burnin_len+unroll_len, batch_size, obs_dim)
            actions = np.stack([seg.actions for seg in segments], axis=1)  # (unroll_len, batch_size)
            rewards = np.stack([seg.rewards for seg in segments], axis=1)  # (unroll_len, batch_size)
            dones = np.stack([seg.dones for seg in segments], axis=1)      # (unroll_len, batch_size)
            last_state = np.stack([seg.last_state for seg in segments])    # (batch_size, obs_dim)
            last_state = tf.expand_dims(last_state, axis=0)                # (1, batch_size, obs_dim)

            states_burnin = states[:self.burnin_len]  # (burnin_len, batch_size, obs_dim)
            states_unroll = states[self.burnin_len:]  # (unroll_len, batch_size, obs_dim)
            next_states_unroll = tf.concat(
                [states[self.burnin_len+1:], last_state], axis=0)

            c0 = tf.convert_to_tensor(
                np.vstack([seg.c_init for seg in segments]), dtype=tf.float32)  # (batch_size, lstm_out_dim)
            h0 = tf.convert_to_tensor(
                np.vstack([seg.h_init for seg in segments]), dtype=tf.float32)  # (batch_size, lstm_out_dim)

            #: burn-in with stored-state
            for t in range(self.burnin_len):
                _, (c0, h0) = self.q_network(states_burnin[t], states=[c0, h0])

            #: Compute TQ
            next_qvalues, c, h = [], c0, h0
            for t in range(self.unroll_len):
                q, (c, h) = self.target_q_network(next_states_unroll[t], states=[c, h])
                next_qvalues.append(q)

            next_qvalues = tf.stack(next_qvalues)                              # (unroll_len, batch_size, action_space)
            next_actions = tf.argmax(next_qvalues, axis=2)                     # (unroll_len, batch_size)
            next_actions_onehot = tf.one_hot(next_actions, self.action_space)  # (unroll_len, batch_size, action_space)
            next_maxQ = tf.reduce_sum(
                next_qvalues * next_actions_onehot, axis=2, keepdims=False)    # (unroll_len, batch_size)
            TQ = rewards + self.gamma * (1 - dones) * next_maxQ                # (unroll_len, batch_size)

            with tf.GradientTape() as tape:
                qvalues, c, h = [], c0, h0
                for t in range(self.unroll_len):
                    q, (c, h) = self.q_network(states_unroll[t], states=[c, h])
                    qvalues.append(q)
                qvalues = tf.stack(qvalues)                                          # (unroll_len, batch_size, action_space)
                actions_onehot = tf.one_hot(actions, self.action_space)
                Q = tf.reduce_sum(qvalues * actions_onehot, axis=2, keepdims=False)  # (unroll_len, batch_size)

                td_errors = TQ - Q
                loss = tf.reduce_mean(tf.square(td_errors), axis=0)
                loss_weighted = tf.reduce_mean(loss * weights)

            grads = tape.gradient(loss_weighted, self.q_network.trainable_variables)
            grads, _ = tf.clip_by_global_norm(grads, 40.0)
            self.optimizer.apply_gradients(
                zip(grads, self.q_network.trainable_variables))

            #: Compute priority
            td_errors_abs = tf.abs(td_errors)
            priorities = self.eta * tf.reduce_max(td_errors_abs, axis=0) \
                + (1 - self.eta) * tf.reduce_mean(td_errors_abs, axis=0)
            priorities = (priorities + 0.001) ** self.alpha

            indices_all += indices
            priorities_all += priorities.numpy().tolist()

        self.target_q_network.set_weights(self.q_network.get_weights())
        current_weights = self.q_network.get_weights()

        return current_weights, indices_all, priorities_all


@ray.remote
class Tester:

    def __init__(self, env_name):

        self.env_name = env_name
        self.action_space = gym.make(self.env_name).action_space.n
        self.q_network = RecurrentQNetwork(self.action_space)
        self.define_network()

    def define_network(self):
        env = gym.make(self.env_name)
        state = env.reset()
        c, h = self.q_network.lstm.get_initial_state(batch_size=1, dtype=tf.float32)
        self.q_network(np.atleast_2d(state), states=[c, h])

    def test_play(self, current_weights, epsilon, monitor_dir=None):

        self.q_network.set_weights(current_weights)

        env = gym.make(self.env_name)
        state = env.reset()
        episode_rewards = 0

        c, h = self.q_network.lstm.get_initial_state(
            batch_size=1, dtype=tf.float32)
        done = False
        while not done:
            action, (next_c, next_h) = self.q_network.sample_action(state, c, h, epsilon)
            next_state, reward, done, _ = env.step(action)
            episode_rewards += reward
            state, c, h = next_state, next_c, next_h

        return episode_rewards


def main(num_actors,
         env_name="CartPole-v0",
         batch_size=16, update_iter=8,
         gamma=0.97, eta=0.9, alpha=0.9,
         burnin_length=4, unroll_length=4):

    s = time.time()

    ray.init(local_mode=False)
    history = []

    epsilons = np.linspace(0.1, 0.8, num_actors) if num_actors > 1 else [0.3]
    actors = [Actor.remote(pid=i, env_name=env_name, epsilon=epsilons[i],
                           gamma=gamma, eta=eta, alpha=alpha,
                           burnin_length=burnin_length,
                           unroll_length=unroll_length)
              for i in range(num_actors)]

    replay = SegmentReplayBuffer(buffer_size=2**12)

    learner = Learner.remote(env_name=env_name, gamma=gamma,
                             eta=eta, alpha=alpha,
                             burnin_length=burnin_length,
                             unroll_length=unroll_length)

    current_weights = ray.get(learner.define_network.remote())
    current_weights = ray.put(current_weights)

    tester = Tester.remote(env_name=env_name)

    wip_actors = [actor.sync_weights_and_rollout.remote(current_weights)
                  for actor in actors]

    for _ in range(10):
        finished, wip_actors = ray.wait(wip_actors, num_returns=1)
        priorities, segments, pid = ray.get(finished[0])
        replay.add(priorities, segments)
        wip_actors.extend(
            [actors[pid].sync_weights_and_rollout.remote(current_weights)])

    # minibatchs: (indices, weights, segments)
    minibatchs = [replay.sample_minibatch(batch_size=batch_size)
                  for _ in range(update_iter)]
    wip_learner = learner.update_network.remote(minibatchs)

    wip_tester = tester.test_play.remote(current_weights, epsilon=0.05)

    minibatchs = [replay.sample_minibatch(batch_size=batch_size)
                  for _ in range(update_iter)]

    learner_cycles = 1
    actor_cycles = 0
    while learner_cycles <= 100:
        actor_cycles += 1
        finished, wip_actors = ray.wait(wip_actors, num_returns=1)
        priorities, segments, pid = ray.get(finished[0])
        replay.add(priorities, segments)
        wip_actors.extend(
            [actors[pid].sync_weights_and_rollout.remote(current_weights)])

        finished_learner, _ = ray.wait([wip_learner], timeout=0)
        if finished_learner:
            current_weights, indices, priorities = ray.get(finished_learner[0])
            wip_learner = learner.update_network.remote(minibatchs)
            current_weights = ray.put(current_weights)

            #: 優先度の更新とminibatchの作成はlearnerよりも十分に速いという前提
            replay.update_priority(indices, priorities)
            minibatchs = [replay.sample_minibatch(batch_size=batch_size)
                          for _ in range(update_iter)]
            print("Actor cycle:", actor_cycles)
            learner_cycles += 1
            actor_cycles = 0

            if learner_cycles % 5 == 0:
                test_score = ray.get(wip_tester)
                print("Cycle:", learner_cycles, "Score:", test_score)
                history.append((learner_cycles-5, test_score))
                wip_tester = tester.test_play.remote(current_weights, epsilon=0.05)

    wallclocktime = round(time.time() - s, 2)
    cycles, scores = zip(*history)
    plt.plot(cycles, scores)
    plt.title(f"total time: {wallclocktime} sec")
    plt.ylabel("test_score(epsilon=0.1)")
    plt.savefig("log/history.png")



if __name__ == '__main__':
    main(num_actors=8)
