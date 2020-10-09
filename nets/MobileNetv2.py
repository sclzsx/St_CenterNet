import torch
import torch.nn as nn
import torch.utils.model_zoo as model_zoo
from collections import OrderedDict
from nets.modules import BasicConv
import math

model_urls = {
    'mobilenet_v2': 'https://download.pytorch.org/models/mobilenet_v2-b0353104.pth',
}


def load_model(model, state_dict):
    new_model = model.state_dict()
    new_keys = list(new_model.keys())
    old_keys = list(state_dict.keys())
    restore_dict = OrderedDict()
    for id in range(len(new_keys)):
        restore_dict[new_keys[id]] = state_dict[old_keys[id]]
    model.load_state_dict(restore_dict)


def _make_divisible(v, divisor, min_value=None):
    """
    This function ensures that all layers have a chanenl number that is divisible by 8
    :param v:
    :param divisor:
    :param min_value:
    :return:
    """
    if min_value is None:
        min_value = divisor

    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    # make sure that round down does not go down by more than 10%
    if new_v < 0.9 * v:
        new_v += divisor

    return new_v


class InvertResidual(nn.Module):
    def __init__(self, inp, outp, stride, expand_ratio):
        super(InvertResidual, self).__init__()
        self.stride = stride
        assert stride in [1, 2]

        hidden_dim = int(round(inp * expand_ratio))
        self.use_res_connect = self.stride == 1 and inp == outp

        layers = []
        if expand_ratio != 1:
            # pw
            layers.append(BasicConv(inp, hidden_dim, kernel_size=1))

        layers.extend([
            # dw
            BasicConv(hidden_dim, hidden_dim, kernel_size=3, stride=stride, groups=hidden_dim, padding=1),
            # pw-linear
            nn.Conv2d(hidden_dim, outp, kernel_size=1, stride=1, padding=0, bias=False),
            nn.BatchNorm2d(outp),
        ])
        self.conv = nn.Sequential(*layers)

    def forward(self, x):
        if self.use_res_connect:
            return x + self.conv(x)
        else:
            return self.conv(x)


class SandGlass(nn.Module):
    def __init__(self, inp, oup, stride, expand_ratio):
        super(SandGlass, self).__init__()
        assert stride in [1, 2]

        hidden_dim = round(inp // expand_ratio)
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


class MobileNetv2(nn.Module):
    def __init__(self, width_mult=1.0, round_nearest=8, ):
        super(MobileNetv2, self).__init__()
        block = SandGlass
        # block = InvertResidual
        input_channel = 32
        inverted_residual_setting = [
            # t, c, n, s
            [1, 16, 1, 1],  # 0
            [6, 24, 2, 2],  # 1
            [6, 32, 3, 2],  # 2
            [6, 64, 4, 2],  # 3
            [6, 96, 3, 1],  # 4
            [6, 160, 3, 2],  # 5
            [6, 320, 1, 1],  # 6
        ]

        self.feat_id = [1, 2, 4, 6]
        self.feat_channel = []

        if len(inverted_residual_setting) == 0 or len(inverted_residual_setting[0]) != 4:
            raise ValueError("inverted_residual_setting should be non-empty"
                             "or a 4-element list, got{}".format(inverted_residual_setting))

        # building first layer
        input_channel = _make_divisible(input_channel * width_mult, round_nearest)
        features = [BasicConv(3, input_channel, kernel_size=3, stride=2, padding=1)]

        # building inverted residual blocks
        for id, (t, c, n, s) in enumerate(inverted_residual_setting):
            ouput_channel = _make_divisible(c * width_mult, round_nearest)
            for i in range(n):
                stride = s if i == 0 else 1
                features.append(block(input_channel, ouput_channel, stride, expand_ratio=t))
                input_channel = ouput_channel

            if id in self.feat_id:
                self.__setattr__("feature_%d" % id, nn.Sequential(*features))
                self.feat_channel.append(ouput_channel)
                features = []

        # weight initialization
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)

    def forward(self, x):
        y = []
        for id in self.feat_id:
            x = self.__getattr__("feature_%d" % id)(x)
            print(x.shape)
            y.append(x)
        return y


def mobilenetv2_10(pretrained=False):
    model = MobileNetv2(width_mult=1.0)
    if pretrained:
        state_dict = model_zoo.load_url(model_urls['mobilenet_v2'])
        load_model(model, state_dict)

    return model


class IDAUp(nn.Module):
    def __init__(self, out_dim, channel):
        super(IDAUp, self).__init__()
        self.up = nn.Sequential(
            nn.ConvTranspose2d(out_dim, out_dim, kernel_size=2, stride=2),
            nn.BatchNorm2d(out_dim, eps=0.001),
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

        for i, channel in enumerate(channels[1:]):
            setattr(self, 'up_%d' % (i), IDAUp(out_dim, channel))

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

        for i in range(len(layers) - 1):
            up = getattr(self, 'up_{}'.format(i))
            x = up([x, layers[i + 1]])
        x = self.conv_last(x)
        return x


class CenterNet(nn.Module):
    def __init__(self, head_conv, num_classes):
        super(CenterNet, self).__init__()
        self.base = mobilenetv2_10()
        channels = self.base.feat_channel
        self.dla_up = MobileNetUp(channels, out_dim=head_conv)
        self.heads = {'hm': num_classes, 'wh': 2, 'reg': 2}

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


if __name__ == "__main__":
    model = CenterNet(head_conv=64, num_classes=2)

    x = torch.randn((10, 3, 512, 512))

    from ptflops import get_model_complexity_info


    # flops,params = get_model_complexity_info(model, (512, 512))

    def hook(self, input, output):
        print(output.data.cpu().numpy().shape)


    for m in model.modules():
        if isinstance(m, nn.Conv2d):
            m.register_forward_hook(hook)

    # y = net(torch.randn(2, 3, 384, 384))
    y = model(torch.randn(2, 3, 512, 512))
