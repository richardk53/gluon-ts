from typing import Sequence, Optional, Union

import torch
from torch.distributions import MultivariateNormal

from inference.smc.normalize import normalize_log_weights
from inference.smc.resampling import (
    EffectiveSampleSizeResampleCriterion,
    systematic_resampling_indices,
    resample,
    make_argmax_log_weights,
)
from models.base_amortized_gls import (
    BaseAmortizedGaussianLinearSystem,
    LatentsRBSMC,
)
from models.base_gls import ControlInputs


class BaseRBSMCGaussianLinearSystem(BaseAmortizedGaussianLinearSystem):
    def __init__(
        self,
        *args,
        resampling_criterion_fn=EffectiveSampleSizeResampleCriterion(
            min_ess_ratio=0.5
        ),
        resampling_indices_fn: callable = systematic_resampling_indices,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.resampling_criterion_fn = resampling_criterion_fn
        self.resampling_indices_fn = resampling_indices_fn

    def loss(
        self,
        past_targets: [Sequence[torch.Tensor], torch.Tensor],
        past_controls: Optional[
            Union[Sequence[ControlInputs], ControlInputs]
        ] = None,
        past_targets_is_observed: Optional[
            Union[Sequence[torch.Tensor], torch.Tensor]
        ] = None,
    ) -> torch.Tensor:
        return self.loss_filter(
            past_targets=past_targets,
            past_controls=past_controls,
            past_targets_is_observed=past_targets_is_observed,
        )

    def loss_filter(
        self,
        past_targets: [Sequence[torch.Tensor], torch.Tensor],
        past_controls: Optional[
            Union[Sequence[ControlInputs], ControlInputs]
        ] = None,
        past_targets_is_observed: Optional[
            Union[Sequence[torch.Tensor], torch.Tensor]
        ] = None,
    ) -> torch.Tensor:
        """
        Computes an estimate of the negative log marginal likelihood.

        Note: the importance weights exp(log_weights) must be un-normalized
        and correspond to the conditional distributions
        (i.e. incremental importance weights / importance weight updates) s.t.
        their product yields an (unbiased) estimate of the marginal likelihood.
        """
        latents_filtered = self.filter(
            past_targets=past_targets,
            past_controls=past_controls,
            past_targets_is_observed=past_targets_is_observed,
        )
        log_weights = [lats.log_weights for lats in latents_filtered]
        log_conditionals = [torch.logsumexp(lws, dim=0) for lws in log_weights]
        log_marginal = sum(log_conditionals)  # FIVO-type ELBO
        return -log_marginal

    def _prepare_forecast(
        self,
        initial_latent: LatentsRBSMC,
        controls: Optional[
            Union[Sequence[ControlInputs], ControlInputs]
        ] = None,
        deterministic: bool = False,
    ):
        cls = initial_latent.variables.__class__

        resampled_log_norm_weights, resampled_tensors = resample(
            n_particle=self.n_particle,
            log_norm_weights=normalize_log_weights(
                log_weights=initial_latent.log_weights
                if not deterministic
                else make_argmax_log_weights(initial_latent.log_weights),
            ),
            tensors_to_resample={
                k: v
                for k, v in initial_latent.variables.__dict__.items()
                if v is not None
            },
            resampling_indices_fn=self.resampling_indices_fn,
            criterion_fn=EffectiveSampleSizeResampleCriterion(
                min_ess_ratio=1.0,  # re-sample always / all.
            ),
        )
        for k, v in initial_latent.variables.__dict__.items():
            if v is None:
                resampled_tensors.update({k: v})

        # pack re-sampled back into object of our API type.
        resampled_initial_latent = initial_latent.__class__(
            log_weights=resampled_log_norm_weights,
            variables=cls(**resampled_tensors,),
            gls_params=None,  # remember to also re-sample these if need to use.
        )
        return resampled_initial_latent, controls

    def smooth_step(
        self,
        lats_smooth_tp1: (LatentsRBSMC, None),
        lats_filter_t: (LatentsRBSMC, None),
    ) -> LatentsRBSMC:
        # TODO: Could do a no-brainer like in the KVAE,
        #  where GLS params are taken from forward.
        #  Similarly, EM loss should actually be the identical analytical loss,
        #  but with better numerical, speed, memory properties due to E-step.
        raise NotImplementedError(
            "Did not develop smoothing yet for this class of models. "
        )
