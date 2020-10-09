"""
Creates a MobileNeXt Model as defined in:
Zhou Daquan, Qibin Hou, Yunpeng Chen, Jiashi Feng, Shuicheng Yan
Rethinking Bottleneck Structure for Efficient Mobile Network Design
arXiv preprint arXiv:2007.02269.
import from https://github.com/d-li14/mobilenetv2.pytorch
"""

import torch.nn as nn
import math

__all__ = ['mobilenext']


def _make_divisible(v, divisor, min_value=None):
    """
    This function is taken from the original tf repo.
    It ensures that all layers have a channel number that is divisible by 8
    It can be seen here:
    https://github.com/tensorflow/models/blob/master/research/slim/nets/mobilenet/mobilenet.py
    :param v:
    :param divisor:
    :param min_value:
    :return:
    """
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


def conv_3x3_bn(inp, oup, stride):
    return nn.Sequential(
        nn.Conv2d(inp, oup, 3, stride, 1, bias=False),
        nn.BatchNorm2d(oup),
        nn.ReLU6(inplace=True)
    )


class SandGlass(nn.Module):
    def __init__(self, inp, oup, stride, reduction_ratio):
        super(SandGlass, self).__init__()
        assert stride in [1, 2]

        hidden_dim = round(inp // reduction_ratio)
        self.identity = stride == 1 and inp == oup

        self.conv = nn.Sequential(
            # dw
            nn.Conv2d(inp, inp, 3, 1, 1, groups=inp, bias=False),
            nn.BatchNorm2d(inp),
            nn.ReLU6(inplace=True),
            # pw-linear
            nn.Conv2d(inp, hidden_dim, 1, 1, 0, bias=False),
            nn.BatchNorm2d(hidden_dim),
            # pw
            nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
            nn.BatchNorm2d(oup),
            nn.ReLU6(inplace=True),
            # dw-linear
            nn.Conv2d(oup, oup, 3, stride, 1, groups=oup, bias=False),
            nn.BatchNorm2d(oup),
        )

    def forward(self, x):
        if self.identity:
            return x + self.conv(x)
        else:
            return self.conv(x)


class MobileNeXt(nn.Module):
    def __init__(self, num_classes=1000, width_mult=1.):
        super(MobileNeXt, self).__init__()
        # setting of sandglass blocks
        self.cfgs = [
            # t, c, n, s
            [2,   96, 1, 2],
            [6,  144, 1, 1],
            [6,  192, 3, 2],
            [6,  288, 3, 2],
            [6,  384, 4, 1],
            [6,  576, 4, 2],
            [6,  960, 3, 1],
            [6, 1280, 1, 1],
        ]

        # building first layer
        input_channel = _make_divisible(32 * width_mult, 8)
        layers = [conv_3x3_bn(3, input_channel, 2)]
        # building inverted residual blocks
        block = SandGlass
        for t, c, n, s in self.cfgs:
            output_channel = _make_divisible(c * width_mult, 8)
            for i in range(n):
                layers.append(block(input_channel, output_channel, s if i == 0 else 1, t))
                input_channel = output_channel
        self.features = nn.Sequential(*layers)
        # building last several layers
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.classifier = nn.Linear(output_channel, num_classes)

        self._initialize_weights()

    def forward(self, x):
        x = self.features(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()

def mobilenetv2_10(pretrained=False):
  model = MobileNeXt(width_mult=1.0)
  if pretrained:
    state_dict = model_zoo.load_url(model_urls['mobilenet_v2'])
    load_model(model, state_dict)

  return model

class IDAUp(nn.Module):
  def __init__(self, out_dim, channel):
    super(IDAUp, self).__init__()
    self.up = nn.Sequential(
      nn.ConvTranspose2d(out_dim, out_dim, kernel_size=2, stride=2),
      nn.BatchNorm2d(out_dim,eps=0.001),
      nn.ReLU()
    )

    self.conv = nn.Sequential(
      nn.Conv2d(channel, out_dim, kernel_size=1, stride=1),
      nn.BatchNorm2d(out_dim, eps=0.001),
      nn.ReLU(inplace=True)
    )

  def forward(self, layers):
    layers = list(layers)

    x = self.up(layers[0])
    y = self.conv(layers[1])

    return x + y

def fill_up_weights(up):
  w = up.weight.data
  f = math.ceil(w.size(2) / 2)
  c = (2 * f - 1 - f % 2) / (2. * f)
  for i in range(w.size(2)):
    for j in range(w.size(3)):
      w[0, 0, i, j] = \
        (1 - math.fabs(i / f - c)) * (1 - math.fabs(j / f - c))
  for c in range(1, w.size(0)):
    w[c, 0, :, :] = w[0, 0, :, :]

class MobileNetUp(nn.Module):
  def __init__(self, channels, out_dim):
    super(MobileNetUp, self).__init__()
    channels = channels[::-1]
    print(channels)
    self.conv = BasicConv(channels[0], out_dim, kernel_size=1, stride=1)

    self.conv_last = BasicConv(out_dim, out_dim, kernel_size=3, stride=1, padding=1)

    for i,channel in enumerate(channels[1:]):
      setattr(self, 'up_%d'%(i), IDAUp(out_dim, channel))

    for m in self.modules():
      if isinstance(m, nn.Conv2d):
        nn.init.kaiming_normal_(m.weight, mode='fan_out')
        if m.bias is not None:
          nn.init.zeros_(m.bias)
      elif isinstance(m, nn.BatchNorm2d):
        nn.init.constant_(m.weight, 1)
        nn.init.constant_(m.bias, 0)
      elif isinstance(m, nn.ConvTranspose2d):
        fill_up_weights(m)

  def forward(self, layers):
    layers = list(layers)
    assert len(layers) > 1

    x = self.conv(layers[0])

    for i in range(len(layers)-1):
      up = getattr(self,'up_{}'.format(i))
      x = up([x, layers[i+1]])
    x = self.conv_last(x)
    return x



class CenterNet(nn.Module):
  def __init__(self, head_conv, num_classes):
    super(CenterNet, self).__init__()
    self.base = mobilenetv2_10()
    channels = self.base.feat_channel
    self.dla_up = MobileNetUp(channels, out_dim=head_conv)
    self.heads = {'hm':num_classes, 'wh':2, 'reg':2}

    for head in self.heads:
      classes = self.heads[head]
      fc = nn.Conv2d(head_conv, classes, kernel_size=1, stride=1, padding=0, bias=True)

      if 'hm' in head:
        fc.bias.data.fill_(-2.19)
      else:
        nn.init.normal_(fc.weight, std=0.001)
        nn.init.constant_(fc.bias, 0)
      self.__setattr__(head, fc)



  def forward(self, x):
    x = self.base(x)
    x = x[::-1]
    x = self.dla_up(x)
    out = []

    for head in self.heads:
      out.append(self.__getattr__(head)(x))

    return out


def get_pose_net(num_layers=18, head_conv=64, num_classes=20):

  model = CenterNet(head_conv=head_conv, num_classes=num_classes)
  return model