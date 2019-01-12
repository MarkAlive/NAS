"""Define blocks.
"""

import mxnet as mx

def channel_shuffle(data, groups):
  data = mx.sym.reshape(data, shape=(0, -4, groups, -1, -2))
  data = mx.sym.swapaxes(data, 1, 2)
  data = mx.sym.reshape(data, shape=(0, -3, -2))

  return data




def test():
    pass

def block_factory_se(input_symbol, num_filter,
                  name, k_size,type,
                  group=1, stride=1, bn=False, bn_mom=0.9,
                  workspace=256,expansion=1,ratio=0.125,se=False,
                  shuffle=False,dim_match =False,memonger=False,
                  **kwargs):
  """Return block symbol.

  Parameters
  ----------
  input : symbol
    input symbol
  input_channels : int
    number of channels of input symbol
  num_filters : int
    output channels
  prefix : str
    prefix string
  kernel_size : tuple
  expansion : int
  group : int
    conv group
  stride : tuple

  """
  assert type in ['bottle_neck','resnet','shape']
  data = input_symbol
  if bn:
    pass

  if type == 'bottle_neck':
    # the same as https://github.com/facebook/fb.resnet.torch#notes, a bit difference with origin paper
    #TODO BN
    bn1 = mx.sym.BatchNorm(data=data, fix_gamma=False, eps=2e-5, momentum=bn_mom, name=name + '_bn1')
    act1 = mx.sym.Activation(data=bn1, act_type='relu', name=name + '_relu1')
    conv1 = mx.sym.Convolution(data=act1, num_filter=int(num_filter*expansion * 0.25), kernel=(1, 1), stride=(1, 1),
                                 pad=(0, 0),num_group=group,
                                 no_bias=True, workspace=workspace, name=name + '_conv1')

    if shuffle:
      data = channel_shuffle(data, group)


    bn2 = mx.sym.BatchNorm(data=conv1, fix_gamma=False, eps=2e-5, momentum=bn_mom, name=name + '_bn2')
    act2 = mx.sym.Activation(data=bn2, act_type='relu', name=name + '_relu2')
    conv2 = mx.sym.Convolution(data=act2, num_filter=int(num_filter * 0.25), kernel=(k_size, k_size), stride=(stride,stride),
                                 pad=(1, 1),
                                 no_bias=True, workspace=workspace, name=name + '_conv2')
    bn3 = mx.sym.BatchNorm(data=conv2, fix_gamma=False, eps=2e-5, momentum=bn_mom, name=name + '_bn3')
    act3 = mx.sym.Activation(data=bn3, act_type='relu', name=name + '_relu3')

    conv3 = mx.sym.Convolution(data=act3, num_filter=num_filter, kernel=(1, 1), stride=(1, 1), pad=(0, 0),
                                 no_bias=True,
                                 workspace=workspace, name=name + '_conv3')
      # TODO(JunQ) type is a key word in python, use another name
    if se:
      squeeze = mx.sym.Pooling(data=conv3, global_pool=True, kernel=(7, 7), pool_type='avg',
                                   name=name + '_squeeze')
      squeeze = mx.symbol.Flatten(data=squeeze, name=name + '_flatten')
      excitation = mx.symbol.FullyConnected(data=squeeze, num_hidden=int(num_filter * ratio),
                                                name=name + '_excitation1')
      excitation = mx.sym.Activation(data=excitation, act_type='relu', name=name + '_excitation1_relu')
      excitation = mx.symbol.FullyConnected(data=excitation, num_hidden=num_filter, name=name + '_excitation2')
      excitation = mx.sym.Activation(data=excitation, act_type='sigmoid', name=name + '_excitation2_sigmoid')
      conv3 = mx.symbol.broadcast_mul(conv3, mx.symbol.reshape(data=excitation, shape=(-1, num_filter, 1, 1)))

    if dim_match:
      shortcut = data
    else:
      shortcut = mx.sym.Convolution(data=act1, num_filter=num_filter, kernel=(1, 1), stride=(stride,stride), no_bias=True,
                                      workspace=workspace, name=name + '_sc')
    if memonger:
      shortcut._set_attr(mirror_stage='True')
    return conv3 + shortcut

  elif type == 'resnet':
    bn1 = mx.sym.BatchNorm(data=data, fix_gamma=False, momentum=bn_mom, eps=2e-5, name=name + '_bn1')
    act1 = mx.sym.Activation(data=bn1, act_type='relu', name=name + '_relu1')

    conv1 = mx.sym.Convolution(data=act1, num_filter=num_filter, kernel=(k_size, k_size), stride=(stride,stride), pad=(1, 1),
                                 no_bias=True, workspace=workspace, name=name + '_conv1')
    bn2 = mx.sym.BatchNorm(data=conv1, fix_gamma=False, momentum=bn_mom, eps=2e-5, name=name + '_bn2')
    act2 = mx.sym.Activation(data=bn2, act_type='relu', name=name + '_relu2')

    conv2 = mx.sym.Convolution(data=act2, num_filter=num_filter, kernel=(k_size, k_size), stride=(1, 1), pad=(1, 1),
                                 no_bias=True, workspace=workspace, name=name + '_conv2')
    if se:
      # implementation of SENet
      squeeze = mx.sym.Pooling(data=conv2, global_pool=True, kernel=(7, 7), pool_type='avg',
                                 name=name + '_squeeze')
      squeeze = mx.symbol.Flatten(data=squeeze, name=name + '_flatten')
      excitation = mx.symbol.FullyConnected(data=squeeze, num_hidden=int(num_filter * ratio),
                                              name=name + '_excitation1')
      excitation = mx.sym.Activation(data=excitation, act_type='relu', name=name + '_excitation1_relu')
      excitation = mx.symbol.FullyConnected(data=excitation, num_hidden=num_filter, name=name + '_excitation2')
      excitation = mx.sym.Activation(data=excitation, act_type='sigmoid', name=name + '_excitation2_sigmoid')
      conv2 = mx.symbol.broadcast_mul(conv2, mx.symbol.reshape(data=excitation, shape=(-1, num_filter, 1, 1)))

    if dim_match:
      shortcut = data
    else:
      shortcut = mx.sym.Convolution(data=data, num_filter=num_filter, kernel=(1, 1), stride=(stride,stride), no_bias=True,
                                       workspace=workspace, name=name + '_sc')

    if memonger:
      shortcut._set_attr(mirror_stage='True')
    return conv2 + shortcut

  elif type == ['shape']:
    bn1 = mx.sym.BatchNorm(data=data, fix_gamma=False, momentum=bn_mom, eps=2e-5, name=name + '_bn1')
    act1 = mx.sym.Activation(data=bn1, act_type='relu', name=name + '_relu1')


    _offset = mx.symbol.Convolution(name=name+'res5a_branch2b_offset', data=act1,
                                                 num_filter=int(2*k_size*k_size*4), pad=(2, 2), kernel=(k_size, k_size), stride=(1, 1),
                                                 dilate=(2, 2), cudnn_off=True)

    data = mx.contrib.symbol.DeformableConvolution(name='res5a_branch2b', data=act1,
                                                             offset=_offset,
                                                             num_filter=512, pad=(2, 2), kernel=(k_size, k_size),
                                                             num_deformable_group=4,
                                                             stride=(1, 1), dilate=(2, 2), no_bias=True)


    bn2 = mx.sym.BatchNorm(data=data, fix_gamma=False, momentum=bn_mom, eps=2e-5, name=name + '_bn2')
    act2 = mx.sym.Activation(data=bn2, act_type='relu', name=name + '_relu2')

    _offset = mx.symbol.Convolution(name=name+'res5a_branch2b_offset', data=act2,
                                    num_filter=int(2 * k_size * k_size * 4), pad=(2, 2), kernel=(k_size, k_size),
                                    stride=(1, 1),
                                    dilate=(2, 2), cudnn_off=True)

    data = mx.contrib.symbol.DeformableConvolution(name=name+'res5a_branch2b', data=act2,
                                                   offset=_offset,
                                                   num_filter=512, pad=(2, 2), kernel=(k_size, k_size),
                                                   num_deformable_group=4,
                                                   stride=(1, 1), dilate=(2, 2), no_bias=True)
    if dim_match:
      shortcut = data
    else:
      shortcut = mx.sym.Convolution(data=data, num_filter=num_filter, kernel=(1, 1), stride=(stride,stride), no_bias=True,
                                       workspace=workspace, name=name + '_sc')

    if memonger:
      shortcut._set_attr(mirror_stage='True')
    return data + shortcut

  else:
      raise("Not Support op %s" %type)

