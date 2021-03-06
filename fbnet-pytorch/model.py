import torch
import torch.nn as nn
import torch.nn.functional as F

import time
import logging

from utils import AvgrageMeter, weights_init, \
                  CosineDecayLR
from data_parallel import DataParallel

class MixedOp(nn.Module):
  """Mixed operation.
  Weighted sum of blocks.
  """
  def __init__(self, blocks):
    super(MixedOp, self).__init__()
    self._ops = nn.ModuleList()
    for op in blocks:
      self._ops.append(op)

  def forward(self, x, weights):
    tmp = []
    for i, op in enumerate(self._ops):
      r = op(x)
      w = weights[..., i].reshape((-1, 1, 1, 1))
      res = w * r
      tmp.append(res)
    return sum(tmp)

class FBNet(nn.Module):

  def __init__(self, num_classes, blocks,
               init_theta=1.0,
               speed_f='./speed.txt',
               alpha=0.2,
               beta=0.6):
    super(FBNet, self).__init__()
    init_func = lambda x: nn.init.constant_(x, init_theta)
    
    self._alpha = alpha
    self._beta = beta
    self._criterion = nn.CrossEntropyLoss().cuda()

    self.theta = []
    self._ops = nn.ModuleList()
    self._blocks = blocks

    tmp = []
    input_conv_count = 0
    for b in blocks:
      if isinstance(b, nn.Module):
        tmp.append(b)
        input_conv_count += 1
      else:
        break
    self._input_conv = nn.Sequential(*tmp)
    self._input_conv_count = input_conv_count
    for b in blocks:
      if isinstance(b, list):
        num_block = len(b)
        theta = nn.Parameter(torch.ones((num_block, )).cuda(), requires_grad=True)
        init_func(theta)
        self.theta.append(theta)
        self._ops.append(MixedOp(b))
        input_conv_count += 1
    tmp = []
    for b in blocks[input_conv_count:]:
      if isinstance(b, nn.Module):
        tmp.append(b)
        input_conv_count += 1
      else:
        break
    self._output_conv = nn.Sequential(*tmp)

    assert len(self.theta) == 22
    with open(speed_f, 'r') as f:
      self._speed = f.readlines()
    self.classifier = nn.Linear(1984, num_classes)

  def forward(self, input, target, temperature=5.0, theta_list=None):
    batch_size = input.size()[0]
    self.batch_size = batch_size
    data = self._input_conv(input)
    theta_idx = 0
    lat = []
    for l_idx in range(self._input_conv_count, len(self._blocks)):
      block = self._blocks[l_idx]
      if isinstance(block, list):
        blk_len = len(block)
        if theta_list is None:
          theta = self.theta[theta_idx]
        else:
          theta = theta_list[theta_idx]
        t = theta.repeat(batch_size, 1)
        weight = nn.functional.gumbel_softmax(t,
                                temperature)
        speed = self._speed[theta_idx].strip().split(' ')[:blk_len]
        speed = [float(tmp) for tmp in speed]
        lat_ = weight * torch.tensor(speed).cuda().repeat(batch_size, 1)
        lat.append(torch.sum(lat_))

        data = self._ops[theta_idx](data, weight)
        theta_idx += 1
      else:
        break

    data = self._output_conv(data)

    lat = torch.tensor(lat).cuda()
    data = nn.functional.avg_pool2d(data, data.size()[2:])
    data = data.reshape((batch_size, -1))
    logits = self.classifier(data)

    self.ce = self._criterion(logits, target).sum()
    self.lat_loss = torch.sum(lat) / batch_size
    self.loss = self.ce +  self._alpha * self.lat_loss.pow(self._beta)

    pred = torch.argmax(logits, dim=1)
    # succ = torch.sum(pred == target).cpu().numpy() * 1.0
    self.acc = torch.sum(pred == target).float() / batch_size
    return self.loss, self.ce, self.lat_loss, self.acc

class Trainer(object):
  """Training network parameters and theta separately.
  """
  def __init__(self, network,
               w_lr=0.01,
               w_mom=0.9,
               w_wd=1e-4,
               t_lr=0.001,
               t_wd=3e-3,
               t_beta=(0.5, 0.999),
               init_temperature=5.0,
               temperature_decay=0.965,
               logger=logging,
               lr_scheduler={'T_max' : 200},
               gpus=[0],
               save_theta_prefix=''):
    assert isinstance(network, FBNet)
    network.apply(weights_init)
    network = network.train().cuda()
    if isinstance(gpus, str):
      gpus = [int(i) for i in gpus.strip().split(',')]
    network = DataParallel(network, gpus)
    self.gpus = gpus
    self._mod = network
    theta_params = network.theta
    mod_params = network.parameters()
    self.theta = theta_params
    self.w = mod_params
    self._tem_decay = temperature_decay
    self.temp = init_temperature
    self.logger = logger
    self.save_theta_prefix = save_theta_prefix

    self._acc_avg = AvgrageMeter('acc')
    self._ce_avg = AvgrageMeter('ce')
    self._lat_avg = AvgrageMeter('lat')
    self._loss_avg = AvgrageMeter('loss')

    self.w_opt = torch.optim.SGD(
                    mod_params,
                    w_lr,
                    momentum=w_mom,
                    weight_decay=w_wd)
    
    self.w_sche = CosineDecayLR(self.w_opt, **lr_scheduler)

    self.t_opt = torch.optim.Adam(
                    theta_params,
                    lr=t_lr, betas=t_beta,
                    weight_decay=t_wd)

  def train_w(self, input, target, decay_temperature=False):
    """Update model parameters.
    """
    self.w_opt.zero_grad()
    loss, ce, lat, acc = self._mod(input, target, self.temp)
    loss.backward()
    self.w_opt.step()
    if decay_temperature:
      tmp = self.temp
      self.temp *= self._tem_decay
      self.logger.info("Change temperature from %.5f to %.5f" % (tmp, self.temp))
    return loss, ce, lat, acc
  
  def train_t(self, input, target, decay_temperature=False):
    """Update theta.
    """
    self.t_opt.zero_grad()
    loss, ce, lat, acc = self._mod(input, target, self.temp)
    loss.backward()
    self.t_opt.step()
    if decay_temperature:
      tmp = self.temp
      self.temp *= self._tem_decay
      self.logger.info("Change temperature from %.5f to %.5f" % (tmp, self.temp))
    return loss, ce, lat, acc
  
  def decay_temperature(self, decay_ratio=None):
    tmp = self.temp
    if decay_ratio is None:
      self.temp *= self._tem_decay
    else:
      self.temp *= decay_ratio
    self.logger.info("Change temperature from %.5f to %.5f" % (tmp, self.temp))
  
  def _step(self, input, target, 
            epoch, step,
            log_frequence,
            func):
    """Perform one step of training.
    """
    input = input.cuda()
    target = target.cuda()
    loss, ce, lat, acc = func(input, target)

    # Get status
    batch_size = self._mod.batch_size

    self._acc_avg.update(acc)
    self._ce_avg.update(ce)
    self._lat_avg.update(lat)
    self._loss_avg.update(loss)

    if step > 1 and (step % log_frequence == 0):
      self.toc = time.time()
      speed = 1.0 * (batch_size * log_frequence) / (self.toc - self.tic)

      self.logger.info("Epoch[%d] Batch[%d] Speed: %.6f samples/sec %s %s %s %s" 
              % (epoch, step, speed, self._loss_avg, 
                 self._acc_avg, self._ce_avg, self._lat_avg))
      map(lambda avg: avg.reset(), [self._loss_avg, self._acc_avg, 
                                    self._ce_avg, self._lat_avg])
      self.tic = time.time()
  
  def search(self, train_w_ds,
            train_t_ds,
            total_epoch=90,
            start_w_epoch=10,
            log_frequence=100):
    """Search model.
    """
    assert start_w_epoch >= 1, "Start to train w"
    self.tic = time.time()
    for epoch in range(start_w_epoch):
      self.logger.info("Start to train w for epoch %d" % epoch)
      for step, (input, target) in enumerate(train_w_ds):
        self._step(input, target, epoch, 
                   step, log_frequence,
                   lambda x, y: self.train_w(x, y, False))
        self.w_sche.step()
        # print(self.w_sche.last_epoch, self.w_opt.param_groups[0]['lr'])

    self.tic = time.time()
    for epoch in range(total_epoch):
      self.logger.info("Start to train theta for epoch %d" % (epoch+start_w_epoch))
      for step, (input, target) in enumerate(train_t_ds):
        self._step(input, target, epoch + start_w_epoch, 
                   step, log_frequence,
                   lambda x, y: self.train_t(x, y, False))
        self.save_theta('./theta-result/%s_theta_epoch_%d.txt' % 
                    (self.save_theta_prefix, epoch+start_w_epoch))
      self.decay_temperature()
      self.logger.info("Start to train w for epoch %d" % (epoch+start_w_epoch))
      for step, (input, target) in enumerate(train_w_ds):
        self._step(input, target, epoch + start_w_epoch, 
                   step, log_frequence,
                   lambda x, y: self.train_w(x, y, False))
        self.w_sche.step()

  def save_theta(self, save_path='theta.txt'):
    """Save theta.
    """
    res = []
    with open(save_path, 'w') as f:
      for t in self.theta:
        t_list = list(t.detach().cpu().numpy())
        res.append(t_list)
        s = ' '.join([str(tmp) for tmp in t_list])
        f.write(s + '\n')
    return res
