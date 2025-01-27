import os
import cv2
import numpy as np
import plotly
from plotly.graph_objs import Scatter
from plotly.graph_objs.scatter import Line
import torch
from torch.nn import functional as F
from typing import Iterable
from torch.nn import Module
from torch import nn


# Plots min, max and mean + standard deviation bars of a population over time
def lineplot(xs, ys_population, title, path='', xaxis='episode'):
    max_colour, mean_colour, std_colour, transparent = 'rgb(0, 132, 180)', 'rgb(0, 172, 237)', 'rgba(29, 202, 255, 0.2)', 'rgba(0, 0, 0, 0)'

    if isinstance(ys_population[0], list) or isinstance(ys_population[0], tuple):
        ys = np.asarray(ys_population, dtype=np.float32)
        ys_min, ys_max, ys_mean, ys_std, ys_median = ys.min(
            1), ys.max(1), ys.mean(1), ys.std(1), np.median(ys, 1)
        ys_upper, ys_lower = ys_mean + ys_std, ys_mean - ys_std

        trace_max = Scatter(x=xs, y=ys_max, line=Line(
            color=max_colour, dash='dash'), name='Max')
        trace_upper = Scatter(x=xs, y=ys_upper, line=Line(
            color=transparent), name='+1 Std. Dev.', showlegend=False)
        trace_mean = Scatter(x=xs, y=ys_mean, fill='tonexty', fillcolor=std_colour, line=Line(
            color=mean_colour), name='Mean')
        trace_lower = Scatter(x=xs, y=ys_lower, fill='tonexty', fillcolor=std_colour, line=Line(
            color=transparent), name='-1 Std. Dev.', showlegend=False)
        trace_min = Scatter(x=xs, y=ys_min, line=Line(
            color=max_colour, dash='dash'), name='Min')
        trace_median = Scatter(x=xs, y=ys_median, line=Line(
            color=max_colour), name='Median')
        data = [trace_upper, trace_mean, trace_lower,
                trace_min, trace_max, trace_median]
    else:
        data = [Scatter(x=xs, y=ys_population, line=Line(color=mean_colour))]
    plotly.offline.plot({
        'data': data,
        'layout': dict(title=title, xaxis={'title': xaxis}, yaxis={'title': title})
    }, filename=os.path.join(path, title + '.html'), auto_open=False)


def write_video(frames, title, path=''):
    frames = np.multiply(np.stack(frames, axis=0).transpose(0, 2, 3, 1), 255).clip(
        0, 255).astype(np.uint8)[:, :, :, ::-1]  # VideoWrite expects H x W x C in BGR
    _, H, W, _ = frames.shape
    writer = cv2.VideoWriter(os.path.join(
        path, '%s.mp4' % title), cv2.VideoWriter_fourcc(*'mp4v'), 30., (W, H), True)
    for frame in frames:
        writer.write(frame)
    writer.release()


def imagine_ahead(prev_state, prev_belief, policy, transition_model, planning_horizon=12):
    '''
    imagine_ahead is the function to draw the imaginary tracjectory using the dynamics model, actor, critic.
    Input: current state (posterior), current belief (hidden), policy, transition_model  # torch.Size([50, 30]) torch.Size([50, 200]) 
    Output: generated trajectory of features includes beliefs, prior_states, prior_means, prior_std_devs
            torch.Size([49, 50, 200]) torch.Size([49, 50, 30]) torch.Size([49, 50, 30]) torch.Size([49, 50, 30])
    '''
    def flatten(x): return x.view([-1]+list(x.size()[2:]))
    prev_belief = flatten(prev_belief)
    prev_state = flatten(prev_state)

    # Create lists for hidden states (cannot use single tensor as buffer because autograd won't work with inplace writes)
    T = planning_horizon
    beliefs, prior_states, prior_means, prior_std_devs = [torch.empty(
        0)] * T, [torch.empty(0)] * T, [torch.empty(0)] * T, [torch.empty(0)] * T
    beliefs[0], prior_states[0] = prev_belief, prev_state

    # Loop over time sequence
    for t in range(T - 1):
        _state = prior_states[t]
        actions = policy.get_action(beliefs[t].detach(), _state.detach())
        # Compute belief (deterministic hidden state)
        hidden = transition_model.act_fn(
            transition_model.fc_embed_state_action(torch.cat([_state, actions], dim=1)))
        beliefs[t + 1] = transition_model.rnn(hidden, beliefs[t])
        # Compute state prior by applying transition dynamics
        hidden = transition_model.act_fn(
            transition_model.fc_embed_belief_prior(beliefs[t + 1]))
        prior_means[t + 1], _prior_std_dev = torch.chunk(
            transition_model.fc_state_prior(hidden), 2, dim=1)
        prior_std_devs[t + 1] = F.softplus(_prior_std_dev) + \
            transition_model.min_std_dev
        prior_states[t + 1] = prior_means[t + 1] + \
            prior_std_devs[t + 1] * torch.randn_like(prior_means[t + 1])
    # Return new hidden states
    # imagined_traj = [beliefs, prior_states, prior_means, prior_std_devs]
    imagined_traj = [torch.stack(beliefs[1:], dim=0), torch.stack(prior_states[1:], dim=0), torch.stack(
        prior_means[1:], dim=0), torch.stack(prior_std_devs[1:], dim=0)]
    return imagined_traj


def lambda_return(imged_reward, value_pred, bootstrap, discount=0.99, lambda_=0.95):
    # Setting lambda=1 gives a discounted Monte Carlo return.
    # Setting lambda=0 gives a fixed 1-step return.
    next_values = torch.cat([value_pred[1:], bootstrap[None]], 0)
    discount_tensor = discount * torch.ones_like(imged_reward)  # pcont
    inputs = imged_reward + discount_tensor * next_values * (1 - lambda_)
    last = bootstrap
    indices = reversed(range(len(inputs)))
    outputs = []
    for index in indices:
        inp, disc = inputs[index], discount_tensor[index]
        last = inp + disc*lambda_*last
        outputs.append(last)
    outputs = list(reversed(outputs))
    outputs = torch.stack(outputs, 0)
    returns = outputs
    return returns


class ActivateParameters:
    def __init__(self, modules: Iterable[Module]):
        """
        Context manager to locally Activate the gradients.
        example:
        ```
        with ActivateParameters([module]):
            output_tensor = module(input_tensor)
        ```
        :param modules: iterable of modules. used to call .parameters() to freeze gradients.
        """
        self.modules = modules
        self.param_states = [
            p.requires_grad for p in get_parameters(self.modules)]

    def __enter__(self):
        for param in get_parameters(self.modules):
            # (param.requires_grad)
            param.requires_grad = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        for i, param in enumerate(get_parameters(self.modules)):
            param.requires_grad = self.param_states[i]

# "get_parameters" and "FreezeParameters" are from the following repo
# https://github.com/juliusfrost/dreamer-pytorch


def get_parameters(modules: Iterable[Module]):
    """
    Given a list of torch modules, returns a list of their parameters.
    :param modules: iterable of modules
    :returns: a list of parameters
    """
    model_parameters = []
    for module in modules:
        model_parameters += list(module.parameters())
    return model_parameters


class FreezeParameters:
    def __init__(self, modules: Iterable[Module]):
        """
        Context manager to locally freeze gradients.
        In some cases with can speed up computation because gradients aren't calculated for these listed modules.
        example:
        ```
        with FreezeParameters([module]):
            output_tensor = module(input_tensor)
        ```
        :param modules: iterable of modules. used to call .parameters() to freeze gradients.
        """
        self.modules = modules
        self.param_states = [
            p.requires_grad for p in get_parameters(self.modules)]

    def __enter__(self):
        for param in get_parameters(self.modules):
            param.requires_grad = False

    def __exit__(self, exc_type, exc_val, exc_tb):
        for i, param in enumerate(get_parameters(self.modules)):
            param.requires_grad = self.param_states[i]


# class Contrastive(nn.Module):
#     def __init__(self):
#         super(Contrastive, self).__init__()

#     @torch.no_grad()
#     def _dequeue_and_enqueue(self, keys):
#         # gather keys before updating queue
#         batch_size = keys.shape[0]
#         print(batch_size)

#         ptr = int(self.queue_ptr)
#         if self.K % batch_size != 0:
#             print(batch_size)
#         assert self.K % batch_size == 0  # for simplicity

#         # replace the keys at ptr (dequeue and enqueue)
#         self.queue[:, ptr:ptr + batch_size] = keys.T
#         ptr = (ptr + batch_size) % self.K  # move pointer

#         self.queue_ptr[0] = ptr

#     def forward(self, im_q, im_k):
#         """
#         Input:
#             im_q: a batch of query images
#             im_k: a batch of key images
#         Output:
#             logits, targets
#         """
#         # compute query features
#         q, features = self.encoder_q(im_q)  # queries: NxC
#         q = nn.functional.normalize(q, dim=1)

#         # compute key features
#         with torch.no_grad():  # no gradient to keys
#             self._momentum_update_key_encoder()  # update the key encoder

#             k, _ = self.encoder_k(im_k)  # keys: NxC
#             k = nn.functional.normalize(k, dim=1)

#         # compute logits
#         # Einstein sum is more intuitive
#         # positive logits: Nx1
#         l_pos = torch.einsum('nc,nc->n', [q, k]).unsqueeze(-1)
#         # negative logits: NxK
#         l_neg = torch.einsum('nc,ck->nk', [q, self.queue.clone().detach()])

#         # logits: Nx(1+K)
#         logits = torch.cat([l_pos, l_neg], dim=1)

#         # apply temperature
#         logits /= self.T

#         # labels: positive key indicators
#         labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

#         # dequeue and enqueue
#         self._dequeue_and_enqueue(k)

#         return logits, labels, features

def contrastive(q, k, device):
    q = F.normalize(q, dim=1)
    k = F.normalize(k, dim=1)
    # compute logits
    # Einstein sum is more intuitive
    # positive logits: Nx1
    # _, z_anch = self.online_net(aug_states_1, log=True)
    # _, z_target = self.momentum_net(aug_states_2, log=True)
    W = nn.Parameter(torch.rand(q.shape[1], q.shape[1])).to(device)
    k_ = torch.matmul(W, k.T)
    logits = torch.matmul(q, k_)
    logits = (logits - torch.max(logits, 1)[0][:, None])
    logits = logits * 0.1 # temperature
    labels = torch.arange(logits.shape[0]).long().to(device=device)
    contrastive_loss = (nn.CrossEntropyLoss()(logits, labels))

    return contrastive_loss