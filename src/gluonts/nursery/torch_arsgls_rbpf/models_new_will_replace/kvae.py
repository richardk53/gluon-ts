from typing import Sequence, Optional, Union, Tuple
from dataclasses import dataclass
from box import Box
import numpy as np
import torch
from torch import nn
from torch.distributions import MultivariateNormal
from utils.utils import \
    make_inv_tril_parametrization, \
    make_inv_tril_parametrization_from_cholesky, \
    TensorDims
from torch_extensions.ops import matvec
from inference.analytical_gausian_linear.inference_sequence_inhomogenous import (
    filter_forward,
    smooth_forward_backward,
    loss_em,
)
from inference.analytical_gausian_linear.inference_step import (
    filter_forward_predictive_distribution,
)
from models_new_will_replace.base_gls import \
    ControlInputs, Latents, Prediction, GLSVariables
from models_new_will_replace.base_amortized_gls import \
    BaseAmortizedGaussianLinearSystem
from models_new_will_replace.gls_parameters.gls_parameters import GLSParameters
from torch_extensions.distributions.parametrised_distribution import (
    ParametrisedMultivariateNormal,
)


@dataclass
class ControlInputsKVAE(ControlInputs):

    encoder: torch.Tensor


@dataclass
class GLSVariablesKVAE(GLSVariables):

    auxiliary: torch.Tensor
    rnn_state: torch.Tensor  # not a RV though


@dataclass
class LatentsKVAE(Latents):

    variables: GLSVariablesKVAE

    def __post_init__(self):
        if hasattr(super(), "__post_init__"):
            super().__post_init__()

        assert isinstance(self.variables, GLSVariablesKVAE)


class KalmanVariationalAutoEncoder(BaseAmortizedGaussianLinearSystem):
    def __init__(
        self,
        *args,
        n_auxiliary: int,
        measurement_model: nn.Module,
        rnn_switch_model: nn.RNNBase,
        reconstruction_weight: float = 1.0,
        **kwargs,
    ):
        kwargs.update({"n_ctrl_target": None})
        super().__init__(*args, **kwargs)
        self.n_auxiliary = n_auxiliary
        self.measurement_model = measurement_model
        self.rnn_switch_model = rnn_switch_model
        self.reconstruction_weight = reconstruction_weight
        self.z_initial = torch.nn.Parameter(torch.zeros(self.n_auxiliary))

    def filter(
        self,
        past_targets: [Sequence[torch.Tensor], torch.Tensor],
        past_controls: Optional[Union[Sequence[ControlInputs], ControlInputs]] = None,
    ) -> Sequence[LatentsKVAE]:
        past_controls = self._expand_particle_dim(past_controls)

        # TODO: when adding back in the missing data, have custom filter loop,
        #  then use V, Q, R directly instead of inv-cholesky-parametrization.
        n_batch = len(past_targets[0])
        state_prior = self.state_prior_model(
            None, batch_shape_to_prepend=(self.n_particle, n_batch),
        )
        LV0inv_tril, LV0inv_logdiag = make_inv_tril_parametrization(
            state_prior.covariance_matrix,
        )
        m0 = state_prior.loc

        # Encode observations y[0:T] to obtain all pseudo-observation z[0:T]
        inv_measurement_dist = self.encoder(past_targets)
        z = inv_measurement_dist.rsample([self.n_particle]).transpose(0, 1)
        # Use as RNN input [z_initial, z[0:T-1]], i.e. previous pseudo-observation.
        z_initial = self.z_initial[None, None, None, :].repeat(
            1, self.n_particle, n_batch, 1,
        )

        # Unroll RNN on all pseudo-obervations to get the SSM params
        rnn_states, rnn_outputs = self.compute_deterministic_switch_sequence(
            rnn_inputs=torch.cat([z_initial, z[:-1]], dim=0),
        )
        gls_params = self.gls_base_parameters(
            switch=rnn_outputs, controls=past_controls,
        )

        # filter with pseudo-obs.
        LQinv_tril, LQinv_logdiag = make_inv_tril_parametrization_from_cholesky(
            gls_params.LQ,
        )
        LRinv_tril, LRinv_logdiag = make_inv_tril_parametrization_from_cholesky(
            gls_params.LR,
        )

        # temporary replacement hack.
        dims = Box(timesteps=len(past_targets), obs=self.n_auxiliary,
                   state=self.n_state, particle=self.n_particle, batch=n_batch)
        m_fw, V_fw = filter_forward(
            dims=dims,
            # contain obs which is auxiliary here.
            A=gls_params.A[:-1],
            B=gls_params.B[:-1] if gls_params.B is not None else None,
            LRinv_tril=LRinv_tril[:-1],
            LRinv_logdiag=LRinv_logdiag[:-1],
            C=gls_params.C,
            D=gls_params.D,
            LQinv_tril=LQinv_tril,
            LQinv_logdiag=LQinv_logdiag,
            LV0inv_tril=LV0inv_tril,
            LV0inv_logdiag=LV0inv_logdiag,
            m0=m0,
            y=z,
            u_state=past_controls.state,
            u_obs=past_controls.target,
        )

        filtered_latents = [
            LatentsKVAE(
                variables=GLSVariablesKVAE(
                    m=m_,
                    V=V_,
                    x=None,
                    auxiliary=z_,
                    rnn_state=h_,
                ),
                gls_params=gps_,
            )
            for (m_, V_, z_, h_, gps_) in zip(m_fw, V_fw, z, rnn_states, gls_params)]
        return filtered_latents

    def smooth(
        self,
        past_targets: [Sequence[torch.Tensor], torch.Tensor],
        past_controls: Optional[Union[Sequence[ControlInputs], ControlInputs]] = None,
    ) -> Sequence[LatentsKVAE]:
        past_controls = self._expand_particle_dim(past_controls)

        smoothed_latents, _ = self._smooth_efficient_tensor(
            past_targets=past_targets,
            past_controls=past_controls,
        )
        return list(iter(smoothed_latents))

    def _smooth_efficient_tensor(
        self,
        past_targets: [Sequence[torch.Tensor], torch.Tensor],
        past_controls: Optional[Union[Sequence[ControlInputs], ControlInputs]] = None,
    ) -> Sequence[LatentsKVAE]:
        # TODO: most of the stuff is duplicated code compared to filter_forward. put in methods

        n_batch = len(past_targets[0])
        state_prior = self.state_prior_model(
            None, batch_shape_to_prepend=(self.n_particle, n_batch)
        )
        LV0inv_tril, LV0inv_logdiag = make_inv_tril_parametrization(
            state_prior.covariance_matrix
        )
        m0 = state_prior.loc

        inv_measurement_dist = self.encoder(past_targets)
        z = inv_measurement_dist.rsample([self.n_particle]).transpose(0, 1)

        z_initial = self.z_initial[None, None, None, :].repeat(
            1, self.n_particle, n_batch, 1,
        )

        # Unroll RNN on all pseudo-obervations to get the SSM params
        rnn_states, rnn_outputs = self.compute_deterministic_switch_sequence(
            rnn_inputs=torch.cat([z_initial, z[:-1]], dim=0),
        )
        gls_params = self.gls_base_parameters(
            switch=rnn_outputs, controls=past_controls,
        )

        LQinv_tril, LQinv_logdiag = make_inv_tril_parametrization(gls_params.Q)
        LRinv_tril, LRinv_logdiag = make_inv_tril_parametrization(gls_params.R)

        dims = Box(timesteps=len(past_targets), target=self.n_auxiliary,
                   state=self.n_state, particle=self.n_particle, batch=n_batch)
        m_fb, V_fb, Cov_fb = smooth_forward_backward(
            dims=dims,
            # contain obs which is auxiliary here.
            A=gls_params.A[:-1],
            B=gls_params.B[:-1] if gls_params.B is not None else None,
            LRinv_tril=LRinv_tril[:-1],
            LRinv_logdiag=LRinv_logdiag[:-1],
            C=gls_params.C,
            D=gls_params.D,
            LQinv_tril=LQinv_tril,
            LQinv_logdiag=LQinv_logdiag,
            LV0inv_tril=LV0inv_tril,
            LV0inv_logdiag=LV0inv_logdiag,
            m0=m0,
            y=z,
            u_state=past_controls.state,
            u_obs=past_controls.target,
        )
        smoothed_latents = LatentsKVAE(
            variables=GLSVariablesKVAE(
                m=m_fb,
                V=V_fb,
                x=None,
                auxiliary=z,
                rnn_state=rnn_states,
            ),
            gls_params=gls_params,
        )
        return smoothed_latents, inv_measurement_dist

    def sample_step(
        self,
        lats_tm1: LatentsKVAE,
        ctrl_t: ControlInputsKVAE,
        deterministic: bool = False,
    ) -> Prediction:
        x_t_dist = torch.distributions.MultivariateNormal(
            loc=(
                    matvec(lats_tm1.gls_params.A, lats_tm1.variables.x)
                    if lats_tm1.gls_params.A is not None
                    else lats_tm1.variables.x
                )
                + (
                    lats_tm1.gls_params.b if lats_tm1.gls_params.b is not None else 0.0),
            scale_tril=lats_tm1.gls_params.LR,
        )
        x_t = x_t_dist.mean if deterministic else x_t_dist.rsample()

        rnn_state_t, rnn_output_t = self.compute_deterministic_switch_step(
            rnn_input=lats_tm1.variables.auxiliary,
            rnn_prev_state=lats_tm1.variables.rnn_state,
        )
        gls_params_t = self.gls_base_parameters(
            switch=rnn_output_t, controls=ctrl_t,
        )

        z_t_dist = torch.distributions.MultivariateNormal(
            loc=matvec(gls_params_t.C, x_t)
                + (gls_params_t.d if gls_params_t.d is not None else 0.0),
            covariance_matrix=gls_params_t.Q,
        )
        z_t = z_t_dist.mean if deterministic else z_t_dist.rsample()

        lats_t = LatentsKVAE(
                variables=GLSVariablesKVAE(
                    m=None,
                    V=None,
                    x=x_t,
                    auxiliary=z_t,
                    rnn_state=rnn_state_t,
                ),
                gls_params=gls_params_t,
            )
        return Prediction(
            latents=lats_t,
            emissions=self.emit(lats_t=lats_t, ctrl_t=ctrl_t),
        )

    def loss(
        self,
        past_targets: [Sequence[torch.Tensor], torch.Tensor],
        past_controls: Optional[Union[Sequence[ControlInputs], ControlInputs]] = None,
        rao_blackwellized=True,
    ) -> torch.Tensor:
        past_controls = self._expand_particle_dim(past_controls)
        if rao_blackwellized:
            return self._loss_em_rb(
                past_targets=past_targets,
                past_controls=past_controls,
            )
        else:
            return self._loss_em_mc(
                past_targets=past_targets,
                past_controls=past_controls,
            )

    def _loss_em_rb(
            self,
            past_targets: [Sequence[torch.Tensor], torch.Tensor],
            past_controls: Optional[Union[Sequence[ControlInputs], ControlInputs]] = None,
    ) -> torch.Tensor:
        """
        Rao-Blackwellization for part of the loss (the EM loss term of the SSM).
        """
        n_batch = len(past_targets[0])

        q = self.encoder(past_targets)
        # SSM pseudo observations.
        # For log_prob evaluation, need particle_first, for RNN time_first.
        z_particle_first = q.rsample([self.n_particle])
        z_time_first = z_particle_first.transpose(0, 1)

        z_initial = self.z_initial[None, None, None, :].repeat(
            1, self.n_particle, n_batch, 1,
        )

        # Unroll RNN on all pseudo-obervations to get the SSM params
        rnn_states, rnn_outputs = self.compute_deterministic_switch_sequence(
            rnn_inputs=torch.cat([z_initial, z_time_first[:-1]], dim=0),
        )
        gls_params = self.gls_base_parameters(
            switch=rnn_outputs, controls=past_controls,
        )

        LQinv_tril, LQinv_logdiag = make_inv_tril_parametrization_from_cholesky(
            gls_params.LQ,
        )
        LRinv_tril, LRinv_logdiag = make_inv_tril_parametrization_from_cholesky(
            gls_params.LR,
        )

        state_prior = self.state_prior_model(
            None, batch_shape_to_prepend=(self.n_particle, n_batch)
        )
        LV0inv_tril, LV0inv_logdiag = make_inv_tril_parametrization(
            state_prior.covariance_matrix
        )
        m0 = state_prior.loc

        dims = Box(timesteps=len(past_targets), target=self.n_auxiliary,
                   state=self.n_state, particle=self.n_particle, batch=n_batch)

        l_em = (
            loss_em(
                dims=dims,
                # contain obs which is auxiliary here.
                A=gls_params.A[:-1],
                B=gls_params.B[:-1] if gls_params.B is not None else None,
                LRinv_tril=LRinv_tril[:-1],
                LRinv_logdiag=LRinv_logdiag[:-1],
                C=gls_params.C,
                D=gls_params.D,
                LQinv_tril=LQinv_tril,
                LQinv_logdiag=LQinv_logdiag,
                LV0inv_tril=LV0inv_tril,
                LV0inv_logdiag=LV0inv_logdiag,
                m0=m0,
                y=z_time_first,
                u_state=past_controls.state,
                u_obs=past_controls.target,
            ).sum(dim=0)
            / dims.particle
        )  # loss_em fn already sums over time. Only avg Particle dim.
        l_measurement = (
            -self.measurement_model(z_particle_first)
            .log_prob(past_targets)
            .sum(dim=(0, 1))
            / dims.particle
        )  # Time and Particle
        l_auxiliary_encoder = (
            q.log_prob(z_particle_first).sum(dim=(0, 1)) / dims.particle
        )  # Time and Particle
        assert all(
            l.shape == l_measurement.shape
            for l in (l_measurement, l_auxiliary_encoder, l_em)
        )
        l_total = (
            self.reconstruction_weight * l_measurement
            + l_auxiliary_encoder
            + l_em
        )
        return l_total

    def _loss_em_mc(
            self,
            past_targets: [Sequence[torch.Tensor], torch.Tensor],
            past_controls: Optional[Union[Sequence[ControlInputs], ControlInputs]] = None,
    ) -> torch.Tensor:
        """" Monte Carlo loss as computed in KVAE paper """
        # past_controls = self._expand_particle_dim(past_controls)
        n_batch = len(past_targets[0])

        # A) SSM related distributions:
        # A1) smoothing.
        latents_smoothed, inv_measurement_dist = self._smooth_efficient_tensor(
            past_targets=past_targets,
            past_controls=past_controls,
        )
        state_smoothed_dist = MultivariateNormal(
            loc=latents_smoothed.variables.m,
            covariance_matrix=latents_smoothed.variables.V,
        )
        x = state_smoothed_dist.rsample()
        gls_params = latents_smoothed.gls_params

        # A2) prior && posterior transition distribution.
        prior_dist = self.state_prior_model(
            None, batch_shape_to_prepend=(self.n_particle, n_batch)
        )

        #  # A, B, R are already 0:T-1.
        transition_dist = MultivariateNormal(
            loc=matvec(gls_params.A[:-1], x[:-1])
            + (
                matvec(gls_params.B[:-1], past_controls.state[:-1])
                if gls_params.B is not None
                else 0.0
            ),
            covariance_matrix=gls_params.R[:-1],
        )
        # A3) posterior predictive (auxiliary) distribution.
        auxiliary_predictive_dist = MultivariateNormal(
            loc=matvec(gls_params.C, x)
            + (
                matvec(gls_params.D, past_controls.target)
                if gls_params.D is not None
                else 0.0
            ),
            covariance_matrix=gls_params.Q,
        )

        # A4) SSM related losses
        l_prior = (
            -prior_dist.log_prob(x[0:1]).sum(dim=(0, 1)) / self.n_particle
        )  # time and particle dim
        l_transition = (
            -transition_dist.log_prob(x[1:]).sum(dim=(0, 1)) / self.n_particle
        )  # time and particle dim
        l_auxiliary = (
            -auxiliary_predictive_dist.log_prob(
                latents_smoothed.variables.auxiliary).sum(dim=(0, 1))
            / self.n_particle
        )  # time and particle dim
        l_entropy = (
            state_smoothed_dist.log_prob(x).sum(dim=(0, 1))  # negative entropy
            / self.n_particle
        )  # time and particle dim

        # B) VAE related distributions
        # B1) inv_measurement_dist already obtained from smoothing (as we dont want to re-compute)
        # B2) measurement (decoder) distribution
        # transpose TPBF -> PTBF to broadcast log_prob of y (TBF) correctly
        z_particle_first = latents_smoothed.variables.auxiliary.transpose(0, 1)
        measurement_dist = self.measurement_model(z_particle_first)
        # B3) VAE related losses
        l_measurement = (
                -measurement_dist.log_prob(past_targets).sum(dim=(0, 1)) / self.n_particle
        )  # time and particle dim
        l_inv_measurement = (
            inv_measurement_dist.log_prob(z_particle_first).sum(dim=(0, 1))
            / self.n_particle
        )  # time and particle dim

        assert all(
            t.shape == l_prior.shape
            for t in (
                l_prior,
                l_transition,
                l_auxiliary,
                l_measurement,
                l_inv_measurement,
            )
        )
        l_total = (
            self.reconstruction_weight * l_measurement
            + l_inv_measurement
            + l_auxiliary
            + l_prior
            + l_transition
            + l_entropy
        )
        return l_total

    def emit(
            self,
            lats_t: LatentsKVAE,
            ctrl_t: ControlInputs,
    ) -> torch.distributions.Distribution:
        return self.measurement_model(lats_t.variables.auxiliary)

    def compute_deterministic_switch_sequence(
            self,
            rnn_inputs: torch.Tensor,
    ) -> Tuple[Sequence[Union[Tuple, torch.Tensor]], torch.Tensor]:
        (T, P, B, F,) = rnn_inputs.shape
        rnn_inputs_flat = rnn_inputs.reshape([T, P * B, F])

        rnn_states = [None] * len(rnn_inputs)
        for t in range(len(rnn_inputs)):
            rnn_state_flat_t = self.rnn_switch_model(
                input=rnn_inputs_flat[t],
                hx=rnn_state_flat_t if t > 0 else None,
            )
            if isinstance(rnn_state_flat_t, Tuple):
                rnn_states[t] = tuple(
                    _h.reshape([P, B, _h.shape[-1]]) for _h in rnn_state_flat_t
                )
            else:
                rnn_states[t] = rnn_state_flat_t.reshape(
                    [P, B, rnn_state_flat_t.shape[-1]],
                )

        if isinstance(rnn_states[0], Tuple):
            rnn_outputs = torch.stack([rnn_states[t][0] for t in range(T)], dim=0)
        else:
            rnn_outputs = torch.stack(rnn_states, dim=0)

        return rnn_states, rnn_outputs

    def compute_deterministic_switch_step(
            self,
            rnn_input: torch.Tensor,
            rnn_prev_state: Union[Tuple[torch.Tensor], torch.Tensor, None],
    ) -> Tuple[Union[Tuple, torch.Tensor], torch.Tensor]:
        (P, B, F,) = rnn_input.shape
        rnn_inputs_flat = rnn_input.reshape([P * B, F])
        if isinstance(rnn_prev_state, Tuple):
            hx_flat = tuple(_h.reshape([P * B, _h.shape[-1]]) for _h in rnn_prev_state)
        else:
            hx_flat = rnn_prev_state.reshape([P * B, rnn_prev_state.shape[-1]])

        h_flat = self.rnn_switch_model(
            input=rnn_inputs_flat,
            hx=hx_flat,
        )
        if isinstance(h_flat, Tuple):
            rnn_state = tuple(_h.reshape([P, B, _h.shape[-1]]) for _h in h_flat)
        else:
            rnn_state = h_flat.reshape([P, B, h_flat.shape[-1]])

        if isinstance(rnn_state, Tuple):
            rnn_output = rnn_state[0]
        else:
            rnn_output = rnn_state
        return rnn_state, rnn_output

    def _prepare_forecast(
            self,
            initial_latent: Latents,
            controls: Optional[
                Union[Sequence[ControlInputs], ControlInputs]] = None,
            deterministic: bool = False,
    ):
        return initial_latent, controls

    def _sample_initial_latents(self, n_particle, n_batch) -> LatentsKVAE:
        state_prior = self.state_prior_model(
            None, batch_shape_to_prepend=(n_particle, n_batch)
        )
        x_initial = state_prior.sample()
        s_initial = None  # initial step has no switch sample.
        raise NotImplementedError("TODO")
        # # TODO: compute gls for pseudo-emission, sample it.
        # #  must check again how I did for the initial transition.
        # #  Forgot how I treated previous rnn state. t-1 or t?
        # initial_latents = LatentsKVAE(
        #     log_weights=torch.zeros_like(state_prior.loc[..., 0]),
        #     gls_params=to-do,  # initial step has none
        #     variables=GLSVariablesKVAE(
        #         x=x_initial,
        #         m=None,
        #         V=None,
        #         auxiliary=to-do,
        #         rnn_state=to-do,
        #     )
        # )
        return initial_latents

    def _expand_particle_dim(self, controls: ControlInputs):
        # assumes we have time dimension
        controls.target = controls.target[:, None, ...] if controls.target is not None else None
        controls.state = controls.state[:, None, ...] if controls.state is not None else None
        controls.switch = controls.switch[:, None, ...] if controls.switch is not None else None
        return controls
    
    def filter_step(
        self,
        lats_tm1: (LatentsKVAE, None),
        tar_t: torch.Tensor,
        ctrl_t: ControlInputs,
    ) -> LatentsKVAE:
        raise NotImplementedError("not needed step-wise in this implementation")