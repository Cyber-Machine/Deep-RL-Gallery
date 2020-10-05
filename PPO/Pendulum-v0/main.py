import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
from pathlib import Path
import shutil
import collections

import gym
from gym import wrappers
import numpy as np
import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt

from env import VecEnv
from models import PolicyNetwork, CriticNetwork
import util


class PPOAgent:

    GAMMA = 0.9

    GAE_LAMBDA = 0.95

    CLIPRANGE = 0.2

    OPT_ITER = 10

    def __init__(self, env_id, action_space,
                 n_envs=1, trajectory_size=200):

        self.env_id = env_id

        self.n_envs = n_envs

        self.trajectory_size = trajectory_size

        self.vecenv = VecEnv(env_id=self.env_id, n_envs=self.n_envs)

        self.policy = PolicyNetwork(action_space=action_space)

        self.critic = CriticNetwork()

        self.r_running_stats = util.RunningStats(shape=(action_space,))

    def run(self, n_updates, logdir):

        self.summary_writer = tf.summary.create_file_writer(str(logdir))

        history = {"steps": [], "scores": []}

        states = self.vecenv.reset()

        hiscore = None

        for epoch in range(n_updates):

            for _ in range(self.trajectory_size):

                actions = self.policy.sample_action(states)

                next_states = self.vecenv.step(actions)

                states = next_states

            trajectories = self.vecenv.get_trajectories()

            for trajectory in trajectories:
                self.r_running_stats.update(trajectory["r"])

            trajectories = self.compute_advantage(trajectories)

            states, actions, advantages, vtargs = self.create_minibatch(trajectories)

            vloss = self.update_critic(states, vtargs)

            self.update_policy(states, actions, advantages)

            global_steps = (epoch+1) * self.trajectory_size * self.n_envs
            train_scores = np.array([traj["r"].sum() for traj in trajectories])

            if epoch % 1 == 0:
                test_scores = np.array(self.play(n=1))
                history["steps"].append(global_steps)
                history["scores"].append(test_scores.mean())
                ma_score = sum(history["scores"][-10:]) / 10
                with self.summary_writer.as_default():
                    tf.summary.scalar("test_score", test_scores.mean(), step=epoch)
                print(f"Epoch {epoch}, {global_steps//1000}K, {test_scores.mean()}")

            if epoch // 10 > 10 and (hiscore is None or ma_score > hiscore):
                self.save_model()
                hiscore = ma_score
                print("Model Saved")

            with self.summary_writer.as_default():
                tf.summary.scalar("value_loss", vloss, step=epoch)
                tf.summary.scalar("train_score", train_scores.mean(), step=epoch)

        return history

    def compute_advantage(self, trajectories):
        """
            Generalized Advantage Estimation (GAE, 2016)
        """

        for trajectory in trajectories:

            trajectory["v_pred"] = self.critic(trajectory["s"]).numpy()

            trajectory["v_pred_next"] = self.critic(trajectory["s2"]).numpy()

            is_nonterminals = 1 - trajectory["done"]

            #normed_rewards = ((trajectory["r"] - self.r_running_stats.mean) / (np.sqrt(self.r_running_stats.var) + 1e-4))
            normed_rewards = (trajectory["r"] / (np.sqrt(self.r_running_stats.var) + 1e-4))

            deltas = normed_rewards + self.GAMMA * is_nonterminals * trajectory["v_pred_next"] - trajectory["v_pred"]

            advantages = np.zeros_like(deltas, dtype=np.float32)

            lastgae = 0
            for i in reversed(range(len(deltas))):
                lastgae = deltas[i] + self.GAMMA * self.GAE_LAMBDA * is_nonterminals[i] * lastgae
                advantages[i] = lastgae

            trajectory["advantage"] = advantages

            trajectory["R"] = advantages + trajectory["v_pred"]

            """経験的return
            trajectory["R"] = np.zeros_like(trajectory["r"])
            R = (1 - trajectory["done"][-1]) * trajectory["v_pred_next"][-1]
            for i in reversed(range(trajectory["r"].shape[0])):
                R = trajectory["r"][i] / reward_std + (1 - trajectory["done"][i]) * self.GAMMA * R
                trajectory["R"][i] = R
            """

        return trajectories

    def update_policy(self, states, actions, advantages):

        for _ in range(self.OPT_ITER):

            old_means, old_stdevs = self.policy(states)

            old_logprob = self.compute_logprob(old_means, old_stdevs, actions)

            with tf.GradientTape() as tape:

                new_means, new_stdevs = self.policy(states)

                new_logprob = self.compute_logprob(new_means, new_stdevs, actions)

                ratio = tf.exp(new_logprob - old_logprob)

                ratio_clipped = tf.clip_by_value(
                    ratio, 1 - self.CLIPRANGE, 1 + self.CLIPRANGE)

                loss_unclipped = ratio * advantages
                loss_clipped = ratio_clipped * advantages

                loss = tf.minimum(loss_unclipped, loss_clipped)
                loss = -1 * tf.reduce_mean(loss)

            grads = tape.gradient(loss, self.policy.trainable_variables)
            grads, _ = tf.clip_by_global_norm(grads, 0.5)
            self.policy.optimizer.apply_gradients(
                zip(grads, self.policy.trainable_variables))

    def update_critic(self, states, v_targs):

        losses = []

        for _ in range(self.OPT_ITER):

            old_vpred = self.critic(states)
            with tf.GradientTape() as tape:
                vpred = self.critic(states)
                vpred_clipped = old_vpred + tf.clip_by_value(
                    vpred - old_vpred, -self.CLIPRANGE, self.CLIPRANGE)
                loss = tf.maximum(tf.square(v_targs - vpred), tf.square(v_targs - vpred_clipped))
                loss = tf.reduce_mean(loss)

            grads = tape.gradient(loss, self.critic.trainable_variables)
            grads, _ = tf.clip_by_global_norm(grads, 0.5)
            self.critic.optimizer.apply_gradients(
                zip(grads, self.critic.trainable_variables))

            losses.append(loss)

        return np.array(losses).mean()

    @tf.function
    def compute_logprob(self, means, stdevs, actions):
        """ガウス分布の確率密度関数よりlogp(x)を計算
            logp(x) = -0.5 log(2π) - log(std)  -0.5 * ((x - mean) / std )^2
        """
        logprob = - 0.5 * np.log(2*np.pi)
        logprob += - tf.math.log(stdevs)
        logprob += - 0.5 * tf.square((actions - means) / stdevs)
        logprob = tf.reduce_sum(logprob, axis=1, keepdims=True)
        return logprob

    def create_minibatch(self, trajectories):

        states = np.vstack([traj["s"] for traj in trajectories])
        actions = np.vstack([traj["a"] for traj in trajectories])

        advantages = np.vstack([traj["advantage"] for traj in trajectories])

        v_targs = np.vstack([traj["R"] for traj in trajectories])

        return states, actions, advantages, v_targs

    def save_model(self):

        self.policy.save_weights("checkpoints/policy")

        self.critic.save_weights("checkpoints/critic")

    def load_model(self):

        self.policy.load_weights("checkpoints/policy")

        self.critic.load_weights("checkpoints/critic")

    def play(self, n=1, monitordir=None, verbose=False):

        if monitordir:
            env = wrappers.Monitor(gym.make(self.env_id),
                                   monitordir, force=True,
                                   video_callable=(lambda ep: True))
        else:
            env = gym.make(self.env_id)

        total_rewards = []

        for _ in range(n):

            state = env.reset()

            done = False

            total_reward = 0

            while not done:

                action = self.policy.sample_action(state)

                if verbose:
                    mean, sd = self.policy(np.atleast_2d(state))
                    print(action, mean.numpy(), sd.numpy())

                next_state, reward, done, _ = env.step(action[0])

                total_reward += reward

                if done:
                    break
                else:
                    state = next_state

            total_rewards.append(total_reward)
            print()
            print(total_reward)
            print()

        return total_rewards


def main(env_id, action_space):

    MONITOR_DIR = Path(__file__).parent / "log"
    LOGDIR = MONITOR_DIR / "summary"
    if LOGDIR.exists():
        shutil.rmtree(LOGDIR)

    agent = PPOAgent(env_id=env_id, action_space=action_space,
                     n_envs=8, trajectory_size=32)

    history = agent.run(n_updates=1500, logdir=LOGDIR)

    plt.plot(history["steps"], history["scores"])
    plt.xlabel("steps")
    plt.ylabel("Total rewards")
    plt.savefig(MONITOR_DIR / "testplay.png")


def testplay(env_id):

    MONITOR_DIR = Path(__file__).parent / "log"

    agent = PPOAgent(env_id=env_id, action_space=1, n_envs=1)
    agent.load_model()
    agent.play(n=5, monitordir=MONITOR_DIR, verbose=True)


if __name__ == "__main__":
    """Todo
        pi_stdの実装確認
    """
    env_id = "Pendulum-v0"
    action_space = 1
    main(env_id, action_space)
    testplay(env_id)

