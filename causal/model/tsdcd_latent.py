import torch
import time
import torch.nn as nn
import torch.distributions as distr
from collections import OrderedDict


class Mask(nn.Module):
    def __init__(self, d: int, d_x: int, tau: int, latent: bool, instantaneous: bool, drawhard: bool):
        super().__init__()

        self.d = d
        self.d_x = d_x
        self.tau = tau
        self.latent = latent
        self.instantaneous = instantaneous
        self.drawhard = drawhard
        self.fixed = False
        self.fixed_output = None
        self.uniform = distr.uniform.Uniform(0, 1)

        if self.latent:
            self.param = nn.Parameter(torch.ones((self.tau, d * d_x, d * d_x)) * 5)
            self.fixed_mask = torch.ones_like(self.param)
            if self.instantaneous:
                # TODO: G[0] or G[-1]
                self.fixed_mask[-1, torch.arange(self.fixed_mask.size(1)), torch.arange(self.fixed_mask.size(2))] = 0
        else:
            if self.instantaneous:
                # initialize mask as log(mask_ij) = 1
                self.param = nn.Parameter(torch.ones((self.tau, d, d, d_x)) * 5)
                self.fixed_mask = torch.ones_like(self.param)
                # set diagonal 0 for G_t0
                self.fixed_mask[-1, torch.arange(self.fixed_mask.size(1)), torch.arange(self.fixed_mask.size(2))] = 0
                # TODO: set neighbors to 0
                # self.fixed_mask[:, :, :, d_x] = 0
            else:
                # initialize mask as log(mask_ij) = 1
                self.param = nn.Parameter(torch.ones((tau, d, d, d_x)) * 5)
                self.fixed_mask = torch.ones_like(self.param)

    def forward(self, b: int, tau: float = 1) -> torch.Tensor:
        """
        :param b: batch size
        :param tau: temperature constant for sampling
        """
        if not self.fixed:
            adj = gumbel_sigmoid(self.param, self.uniform, b, tau=tau, hard=self.drawhard)
            adj = adj * self.fixed_mask
            return adj
        else:
            assert self.fixed_output is not None
            return self.fixed_output.repeat(b, 1, 1, 1)

    def get_proba(self) -> torch.Tensor:
        if not self.fixed:
            return torch.sigmoid(self.param) * self.fixed_mask
        else:
            return self.fixed_output

    def fix(self, fixed_output):
        self.fixed_output = fixed_output
        self.fixed = True


class MixingMask(nn.Module):
    def __init__(self, d: int, d_x: int, d_z: int, gt_mask=None):
        super().__init__()
        if gt_mask is not None:
            self.param = (gt_mask > 0) * 10.
        else:
            self.param = nn.Parameter(torch.ones(d, d_x, d_z) * 5)

    def forward(self, batch_size):
        param = self.param.unsqueeze(0).repeat(batch_size, 1, 1, 1)
        mask = nn.functional.gumbel_softmax(param, tau=1, hard=False)
        return mask


def sample_logistic(shape, uniform):
    u = uniform.sample(shape)
    return torch.log(u) - torch.log(1 - u)


def gumbel_sigmoid(log_alpha, uniform, bs, tau=1, hard=False):
    shape = tuple([bs] + list(log_alpha.size()))
    logistic_noise = sample_logistic(shape, uniform)

    y_soft = torch.sigmoid((log_alpha + logistic_noise) / tau)

    if hard:
        y_hard = (y_soft > 0.5).type(torch.Tensor)

        # This weird line does two things:
        #   1) at forward, we get a hard sample.
        #   2) at backward, we differentiate the gumbel sigmoid
        y = y_hard.detach() - y_soft.detach() + y_soft

    else:
        y = y_soft

    return y


class MLP(nn.Module):
    def __init__(self,
                 num_layers: int,
                 num_hidden: int,
                 num_input: int,
                 num_output: int):
        super().__init__()
        self.num_layers = num_layers
        self.num_hidden = num_hidden
        self.num_input = num_input
        self.num_output = num_output

        module_dict = OrderedDict()

        # create model layer by layer
        in_features = num_input
        out_features = num_hidden
        if num_layers == 0:
            out_features = num_output

        module_dict['lin0'] = nn.Linear(in_features, out_features)

        for layer in range(num_layers):
            in_features = num_hidden
            out_features = num_hidden

            if layer == num_layers - 1:
                out_features = num_output

            module_dict[f'nonlin{layer}'] = nn.LeakyReLU()
            module_dict[f'lin{layer+1}'] = nn.Linear(in_features, out_features)

        self.model = nn.Sequential(module_dict)

    def forward(self, x) -> torch.Tensor:
        return self.model(x)


class LatentTSDCD(nn.Module):
    """Differentiable Causal Discovery for time series with latent variables"""
    def __init__(self,
                 num_layers: int,
                 num_hidden: int,
                 num_input: int,
                 num_output: int,
                 coeff_kl: float,
                 distr_z0: str,
                 distr_encoder: str,
                 distr_transition: str,
                 distr_decoder: str,
                 d: int,
                 d_x: int,
                 d_z: int,
                 tau: int,
                 instantaneous: bool,
                 nonlinear_mixing: bool,
                 hard_gumbel: bool,
                 no_gt: bool,
                 debug_gt_graph: bool,
                 debug_gt_z: bool,
                 debug_gt_w: bool,
                 gt_graph: torch.tensor = None,
                 gt_w: torch.tensor = None,
                 tied_w: bool = False):
        """
        Args:
            num_layers: number of layers of each MLP
            num_hidden: number of hidden units of each MLP
            num_input: number of inputs of each MLP
            num_output: number of inputs of each MLP
            coeff_kl: coefficient of the KL term

            distr_z0: distribution of the first z (gaussian)
            distr_encoder: distribution parametrized by the encoder (gaussian)
            distr_transition: distribution parametrized by the transition model (gaussian)
            distr_decoder: distribution parametrized by the decoder (gaussian)

            d: number of features
            d_x: number of grid locations
            d_z: number of latent variables
            tau: size of the timewindow
            instantaneous: if True, models instantaneous connections
            hard_gumbel: if True, use hard sampling for the masks

            no_gt: if True, do not use any ground-truth data (useful with realworld dataset)
            debug_gt_graph: if True, set the masks to the ground-truth graphes (gt_graph)
            debug_gt_z: if True, use directly the ground-truth z (gt_z sampled with the data)
            debug_gt_w: if True, set the matrices W to the ground-truth W (gt_w)
            gt_graph: Ground-truth graphes, only used if debug_gt_graph is True
            gt_w: Ground-truth W, only used if debug_gt_w is True
        """
        super().__init__()

        # nn encoder hyperparameters
        self.num_layers = num_layers
        self.num_hidden = num_hidden
        self.num_input = num_input
        self.num_output = num_output
        self.coeff_kl = coeff_kl

        self.d = d
        self.d_x = d_x
        self.d_z = d_z
        self.tau = tau
        self.instantaneous = instantaneous
        self.nonlinear_mixing = nonlinear_mixing
        self.hard_gumbel = hard_gumbel
        self.no_gt = no_gt
        self.debug_gt_graph = debug_gt_graph
        self.debug_gt_z = debug_gt_z
        self.debug_gt_w = debug_gt_w
        self.tied_w = tied_w

        if self.instantaneous:
            self.total_tau = tau + 1
        else:
            self.total_tau = tau

        if self.no_gt:
            self.gt_w = None
            self.gt_graph = None
        else:
            self.gt_w = torch.tensor(gt_w).double()
            self.gt_graph = torch.tensor(gt_graph).double()

        if distr_z0 == "gaussian":
            self.distr_z0 = torch.normal
        else:
            raise NotImplementedError("This distribution is not implemented yet.")

        if distr_transition == "gaussian":
            self.distr_transition = torch.normal
        else:
            raise NotImplementedError("This distribution is not implemented yet.")

        if distr_encoder == "gaussian":
            self.distr_encoder = torch.normal
        else:
            raise NotImplementedError("This distribution is not implemented yet.")

        if distr_decoder == "gaussian":
            self.distr_decoder = distr.normal.Normal
        else:
            raise NotImplementedError("This distribution is not implemented yet.")

        # self.encoder_decoder = EncoderDecoder(self.d, self.d_x, self.d_z, self.nonlinear_mixing, 4, 1, self.debug_gt_w, self.gt_w, self.tied_w)
        if self.nonlinear_mixing:
            num_hidden_mixing = 8
            num_layer_mixing = 2
            self.autoencoder = NonLinearAutoEncoder(d, d_x, d_z,
                                                    num_hidden_mixing,
                                                    num_layer_mixing,
                                                    use_gumbel_mask=True,
                                                    tied=True,
                                                    gt_w=self.gt_w)
            # self.encoder = NonlinearEncoder(d, d_x, d_z, num_hidden_mixing, num_layer_mixing, True)
            # self.decoder = NonlinearEncoder(d, d_z, d_x, num_hidden_mixing, num_layer_mixing, True)
        else:
            self.autoencoder = Autoencoder(d, d_x, d_z, tied=False)
            # self.encoder = LinearEncoder(d, d_x, d_z)
            # self.decoder = LinearEncoder(d, d_z, d_x)

        if debug_gt_w:
            self.decoder.w = gt_w

        self.transition_model = TransitionModel(self.d, self.d_z, self.total_tau,
                                                self.num_layers,
                                                self.num_hidden,
                                                self.num_output)

        self.mask = Mask(d, d_z, self.total_tau, instantaneous=instantaneous, latent=True, drawhard=hard_gumbel)
        if self.debug_gt_graph:
            if self.instantaneous:
                self.mask.fix(self.gt_graph)
            else:
                self.mask.fix(self.gt_graph[:-1])

    def get_adj(self):
        """
        Returns: Matrices of the probabilities from which the masks linking the
        latent variables are sampled
        """
        return self.mask.get_proba()

    def encode(self, x, y):
        """
        encode X and Y into latent variables Z
        """
        b = x.size(0)
        z = torch.zeros(b, self.tau + 1, self.d, self.d_z)
        mu = torch.zeros(b, self.d, self.d_z)
        std = torch.zeros(b, self.d, self.d_z)

        # sample Zs
        for i in range(self.d):
            # get params from the encoder q(z^t | x^t)
            for t in range(self.tau):
                # q_mu, q_logvar = self.encoder_decoder(x[:, t, i], i, encoder=True)  # torch.matmul(self.W, x)
                q_mu, q_logvar = self.autoencoder(x[:, t, i], i, encode=True)

                # reparam trick
                q_std = torch.exp(0.5 * q_logvar)
                z[:, t, i] = q_mu + q_std * self.distr_encoder(0, 1, size=q_mu.size())

            # q_mu, q_logvar = self.encoder_decoder(y[:, i], i, encoder=True)  # torch.matmul(self.W, x)
            q_mu, q_logvar = self.autoencoder(y[:, i], i, encode=True)

            q_std = torch.exp(0.5 * q_logvar)
            z[:, -1, i] = q_mu + q_std * self.distr_encoder(0, 1, size=q_mu.size())
            mu[:, i] = q_mu
            std[:, i] = q_std

        return z, mu, std

    def transition(self, z, mask):
        b = z.size(0)
        mu = torch.zeros(b, self.d, self.d_z)
        std = torch.zeros(b, self.d, self.d_z)

        # learning conditional variance
        # for i in range(self.d):
        #     pz_params = torch.zeros(b, self.d_z, 2)
        #     for k in range(self.d_z):
        #         pz_params[:, k] = self.transition_model(z, mask[:, :, i * self.d_z + k], i, k)
        #     mu[:, i] = pz_params[:, :, 0]
        #     std[:, i] = torch.exp(0.5 * pz_params[:, :, 1])

        for i in range(self.d):
            pz_params = torch.zeros(b, self.d_z, 1)
            for k in range(self.d_z):
                pz_params[:, k] = self.transition_model(z, mask[:, :, i * self.d_z + k], i, k)
            mu[:, i] = pz_params[:, :, 0]
            std[:, i] = torch.exp(0.5 * self.transition_model.logvar[i])

        return mu, std

    def decode(self, z):
        mu = torch.zeros(z.size(0), self.d, self.d_x)
        std = torch.zeros(z.size(0), self.d, self.d_x)

        for i in range(self.d):
            # px_mu, px_logvar = self.encoder_decoder(z[:, i], i, encoder=False)
            px_mu, px_logvar = self.autoencoder(z[:, i], i, encode=False)
            mu[:, i] = px_mu
            std[:, i] = torch.exp(0.5 * px_logvar)

        return mu, std

    def forward(self, x, y, gt_z, iteration):
        b = x.size(0)

        # sample Zs (based on X)
        z, q_mu_y, q_std_y = self.encode(x, y)

        if self.debug_gt_z:
            z = gt_z

        # get params of the transition model p(z^t | z^{<t})
        mask = self.mask(b)
        if self.instantaneous:
            pz_mu, pz_std = self.transition(z.clone(), mask)
        else:
            pz_mu, pz_std = self.transition(z[:, :-1].clone(), mask)

        # get params from decoder p(x^t | z^t)
        px_mu, px_std = self.decode(z[:, -1])

        # set distribution with obtained parameters
        p = distr.Normal(pz_mu.view(b, -1), pz_std.view(b, -1))
        q = distr.Normal(q_mu_y.view(b, -1), q_std_y.view(b, -1))
        px_distr = self.distr_decoder(px_mu, px_std)

        # compute the KL, the reconstruction and the ELBO
        # kl = distr.kl_divergence(q, p).mean()
        kl_raw = 0.5 * (torch.log(pz_std**2) - torch.log(q_std_y**2)) + 0.5 * (q_std_y**2 + (q_mu_y - pz_mu) ** 2) / pz_std**2 - 0.5
        kl = torch.sum(kl_raw, dim=[2]).mean()
        # kl = torch.sum(0.5 * (torch.log(pz_std**2) - torch.log(q_std_y**2)) + 0.5 * (q_std_y**2 + (q_mu_y - pz_mu) ** 2) / pz_std**2 - 0.5, dim=[1, 2]).mean()
        assert kl >= 0, f"KL={kl} has to be >= 0"

        recons = torch.mean(torch.sum(px_distr.log_prob(y), dim=[1, 2]))
        elbo = recons - self.coeff_kl * kl

        return elbo, recons, kl, px_mu

    def get_kl(self, mu1, sigma1, mu2, sigma2) -> float:
        """KL between two multivariate Gaussian Q and P.
        Here, Q is spherical and P is diagonal"""
        kl = 0.5 * (torch.log(torch.prod(sigma2, dim=1) / torch.prod(sigma1, dim=1)) +
                    torch.sum(sigma1 / sigma2, dim=1) - self.d_z +
                    torch.einsum('bd, bd -> b', (mu2 - mu1) * (1 / sigma2), mu2 - mu1))
        # kl = 0.5 * (torch.log(torch.prod(sigma2, dim=1) / sigma1 ** self.d_z) +
        #             torch.sum(sigma1 / sigma2, dim=1) - self.d_z +
        #             torch.einsum('bd, bd -> b', (mu2 - mu1) * (1 / sigma2), mu2 - mu1))
        if torch.sum(kl) < 0:
            __import__('ipdb').set_trace()
            print(sigma2 ** self.d_z)
            print(torch.prod(sigma1, dim=1))
            print(torch.sum(torch.log(sigma2 ** self.d_z / torch.prod(sigma1, dim=1))))
            print(torch.sum(torch.sum(sigma1 / sigma2, dim=1)))
            # print(torch.sum(torch.einsum('bd, bd -> b', (mu2 - mu1) * (1 / s_p), mu2 - mu1)))

        return torch.sum(kl)

class LinearAutoEncoder(nn.Module):
    def __init__(self, d, d_x, d_z, tied):
        super().__init__()
        self.d_x = d_x
        self.d_z = d_z
        self.tied = tied
        unif = (1 - 0.1) * torch.rand(size=(d, d_z, d_x)) + 0.1
        self.w = nn.Parameter(unif / torch.tensor(d_x))
        if not tied:
            unif = (1 - 0.1) * torch.rand(size=(d, d_x, d_z)) + 0.1
            self.w_encoder = nn.Parameter(unif / torch.tensor(d_z))

        self.logvar_encoder = nn.Parameter(torch.ones(d) * -1)
        self.logvar_decoder = nn.Parameter(torch.ones(d) * -1)

    def get_w_encoder(self):
        if self.tied:
            return self.w_encoder
        else:
            # TODO: swapaxes?
            return self.w

    def get_w_decoder(self):
        return self.w

    def encode(self, x, i):
        if self.tied:
            w = self.w[i].T
        else:
            w = self.w_encoder[i]
        mu = torch.matmul(x, w)
        return mu, self.logvar_encoder

    def decode(self, z, i):
        w = self.w[i]
        mu = torch.matmul(z, w)
        return mu, self.logvar_decoder

    def forward(self, x, i, encode: bool = False):
        if encode:
            return self.encode(x, i)
        else:
            return self.decode(x, i)

class LinearEncoder(nn.Module):
    def __init__(self, d, num_input, num_output):
        super().__init__()
        unif = (1 - 0.1) * torch.rand(size=(d, num_output, num_input)) + 0.1
        self.w = nn.Parameter(unif / torch.tensor(num_input))
        self.logvar = nn.Parameter(torch.ones(d) * -4)
        # self.logvar = torch.tensor([1.])

    def forward(self, x, i):
        w = self.w[i]
        mu = torch.matmul(x, w)
        return mu, self.logvar


class NonLinearAutoEncoder(nn.Module):
    def __init__(self, d, d_x, d_z, num_hidden, num_layer, use_gumbel_mask, tied, gt_w=None):
        super().__init__()
        if use_gumbel_mask:
            self.use_grad_project = False
        else:
            self.use_grad_project = True
        self.d_x = d_x
        self.d_z = d_z
        self.tied = tied
        self.use_gumbel_mask = use_gumbel_mask

        if self.use_gumbel_mask:
            self.mask = MixingMask(d, d_x, d_z, gt_w)
            if not tied:
                self.mask_encoder = MixingMask(d, d_x, d_z, gt_w)
        else:
            unif = (1 - 0.1) * torch.rand(size=(d, d_x, d_z)) + 0.1
            self.w = nn.Parameter(unif / torch.tensor(d_z))
            if not tied:
                unif = (1 - 0.1) * torch.rand(size=(d, d_z, d_x)) + 0.1
                self.w_encoder = nn.Parameter(unif / torch.tensor(d_x))

        self.encoder = nn.ModuleList(MLP(num_layer, num_hidden, d_x, 1) for i in range(d_z))
        self.decoder = nn.ModuleList(MLP(num_layer, num_hidden, d_z, 1) for i in range(d_x))
        self.logvar_encoder = nn.Parameter(torch.ones(d) * -1)
        self.logvar_decoder = nn.Parameter(torch.ones(d) * -1)

    def get_w_encoder(self):
        if self.use_gumbel_mask:
            if self.tied:
                return torch.transpose(self.mask.param, 1, 2)
            else:
                return torch.transpose(self.mask_encoder.param, 1, 2)
        else:
            if self.tied:
                return self.w
            else:
                return self.w_encoder

    def get_w_decoder(self):
        if self.use_gumbel_mask:
            return self.mask.param
        else:
            return self.w

    def encode(self, x, i):
        mu = torch.zeros((x.shape[0], self.d_z))
        if self.use_gumbel_mask:
            if self.tied:
                sampled_mask = self.mask(x.shape[0])
                # TODO: swap axes
            else:
                sampled_mask = self.mask_encoder(x.shape[0])

        for j in range(self.d_z):
            if self.use_gumbel_mask:
                mask = sampled_mask[:, i, :, j]
            else:
                if self.tied:
                    mask = self.w[i, j]
                else:
                    mask = self.w_encoder[i, j]
            mu[:, j] = self.encoder[j](mask * x).squeeze()
        return mu, self.logvar_encoder

    def decode(self, z, i):
        mu = torch.zeros((z.shape[0], self.d_x))
        if self.use_gumbel_mask:
            sampled_mask = self.mask(z.shape[0])

        for j in range(self.d_x):
            if self.use_gumbel_mask:
                mask = sampled_mask[:, i, j]
            else:
                mask = self.w[i, j]
            mu[:, j] = self.decoder[j](mask * z).squeeze()
        return mu, self.logvar_decoder


    def forward(self, x, i, encode: bool = False):
        if encode:
            return self.encode(x, i)
        else:
            return self.decode(x, i)


class NonlinearEncoder(nn.Module):
    def __init__(self, d, num_input, num_output, num_hidden, num_layers, use_gumbel_mask):
        super().__init__()
        self.num_output = num_output
        self.use_gumbel_mask = use_gumbel_mask

        if self.use_gumbel_mask:
            self.mask = MixingMask(d, num_output, num_input)
        else:
            unif = (1 - 0.1) * torch.rand(size=(d, num_output, num_input)) + 0.1
            self.w = nn.Parameter(unif / torch.tensor(num_input))
        unif = (1 - 0.1) * torch.rand(size=(d, num_output, num_input)) + 0.1
        self.w = nn.Parameter(unif / torch.tensor(num_input))

        self.nonlinear_encoder = nn.ModuleList(MLP(num_layers, num_hidden, num_input, 1) for i in range(num_output))
        self.logvar = nn.Parameter(torch.ones(d) * -4)
        # self.logvar = torch.tensor([1.])

    def forward(self, x, i):
        mu = torch.zeros((x.shape[0], self.num_output))
        if self.use_gumbel_mask:
            sampled_mask = self.mask(x.shape[0])

        for j in range(self.num_output):
            if self.use_gumbel_mask:
                mask = sampled_mask[:, i, j]
            else:
                mask = self.w[i, j]
            mu[:, j] = self.nonlinear_encoder[j](mask * x).squeeze()
        return mu, self.logvar


# class EncoderDecoder(nn.Module):
#     """Combine an encoder and a decoder, particularly useful when W is a shared
#     parameter."""
#     def __init__(self, d: int, d_x: int, d_z: int, nonlinear: bool = False,
#                  num_hidden: int = 4, num_layers: int = 1, debug_gt_w: bool = False, gt_w:
#                  torch.tensor = None, tied_w: bool = False):
#         """
#         Args:
#             d: number of features
#             d_x: dimensionality of grid locations
#             d_z: dimensionality of latent variables
#             nonlinear: if True, use nonlinear function in addition
#                to the matrix W
#             debug_gt_w: if True, set W as gt_w
#             gt_w: ground-truth W
#             tied_w: if True, the encoder use the transposed
#             of the matrix W from the decoder
#         """
#         super().__init__()
#         self.use_grad_projection = True
#         self.tied_w = tied_w
#         self.nonlinear = nonlinear
# 
#         self.d = d
#         self.d_x = d_x
#         self.d_z = d_z
#         self.debug_gt_w = debug_gt_w
#         self.gt_w = gt_w
# 
#         unif = (1 - 0.4) * torch.rand(size=(d, d_x, d_z)) + 0.4
#         if self.use_grad_projection:
#             self.w = nn.Parameter(unif / torch.tensor(self.d_z))
#         else:
#             # otherwise, self.w is the log of W
#             self.w = nn.Parameter(torch.log(unif) - torch.log(torch.tensor(self.d_z)))
# 
#         if self.tied_w:
#             self.w_q = None
#         else:
#             unif = (1 - 0.1) * torch.rand(size=(d, d_x, d_z)) + 0.1
#             self.w_q = nn.Parameter(unif / torch.tensor(self.d_z))
# 
#         self.logvar_encoder = nn.Parameter(torch.ones(d) * -4)
#         self.logvar_decoder = nn.Parameter(torch.ones(d) * -4)
# 
#         if self.nonlinear:
#             self.nonlinear_encoder = nn.ModuleList(MLP(num_layers, num_hidden, d_x, 1) for i in range(d_z))
#             self.nonlinear_decoder = nn.ModuleList(MLP(num_layers, num_hidden, d_z, 1) for i in range(d_x))
#         else:
#             self.nonlinear_encoder = None
#             self.nonlinear_decoder = None
# 
#     def get_w(self, encoder: bool = False) -> torch.tensor:
#         if self.tied_w:
#             if self.debug_gt_w:
#                 w = self.gt_w
#             elif self.use_grad_projection:
#                 w = self.w
#             else:
#                 w = torch.exp(self.w)
#         else:
#             if encoder:
#                 w = self.w_q
#             else:
#                 if self.debug_gt_w:
#                     w = self.gt_w
#                 else:
#                     w = self.w
# 
#         return w
# 
#     def forward(self, x, i, encoder: bool):
#         w = self.get_w(encoder)[i]  # dim: (d_x, d_z)
# 
#         if encoder:
#             # encoder q(z | x)
#             mu = torch.zeros((x.shape[0], self.d_z))
#             if self.nonlinear:
#                 for j in range(self.d_z):
#                     mu[:, j] = self.nonlinear_encoder[j](w[:, j] * x).squeeze()
#             else:
#                 mu = torch.matmul(x, w)
#             logvar = self.logvar_encoder[i]
#         else:
#             z = x
#             mu = torch.zeros((x.shape[0], self.d_x))
#             # decoder p(x | z)
#             # if self.tied_w:
#             if self.nonlinear:
#                 for j in range(self.d_x):
#                     mu[:, j] = self.nonlinear_decoder[j](w[j] * z).squeeze()
#             else:
#                 w = w.T
#                 mu = torch.matmul(z, w)
#             logvar = self.logvar_decoder[i]
#         return mu, logvar
# 
#     def project_gradient(self):
#         assert self.use_grad_projection
#         with torch.no_grad():
#             self.w.clamp_(min=0.)
#         assert torch.min(self.w) >= 0.


class TransitionModel(nn.Module):
    """ Models the transitions between the latent variables Z with neural networks.  """
    def __init__(self, d: int, d_z: int, tau: int, num_layers: int, num_hidden:
                 int, num_output: int = 2):
        """
        Args:
            d: number of features
            d_z: number of latent variables
            tau: size of the timewindow
            num_layers: number of layers for the neural networks
            num_hidden: number of hidden units
            num_output: number of outputs
        """
        super().__init__()
        self.d = d
        self.d_z = d_z
        self.tau = tau
        output_var = False

        # initialize NNs
        self.num_layers = num_layers
        self.num_hidden = num_hidden
        if output_var:
            self.num_output = num_output
        else:
            self.num_output = 1
            # self.logvar = torch.ones(1)  * 0. # nn.Parameter(torch.ones(d) * 0.1)
            # self.logvar = nn.Parameter(torch.ones(d) * -4)
            self.logvar = nn.Parameter(torch.ones(d, d_z) * -4)

        self.nn = nn.ModuleList(MLP(num_layers, num_hidden, d * d_z * tau, self.num_output) for i in range(d * d_z))
        # self.nn = MLP(num_layers, num_hidden, d * k * k, self.num_output)

    def forward(self, z, mask, i, k):
        """Returns the params of N(z_t | z_{<t}) for a specific feature i and latent variable k
        NN(G_{tau-1} * z_{t-1}, ..., G_{tau-k} * z_{t-k})
        """
        # t_total = torch.max(self.tau, z_past.size(1))  # TODO: find right dim
        # param_z = torch.zeros(z_past.size(0), 2)
        z = z.view(mask.size())
        masked_z = (mask * z).view(z.size(0), -1)

        param_z = self.nn[i * self.d_z + k](masked_z)
        # param_z = self.nn(masked_z)

        return param_z
