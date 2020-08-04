import os
from typing import Optional, Sequence
from box import Box
from torch.optim import Adam


import numpy as np
import torch
from torch.utils.data import DataLoader
from torch.optim.optimizer import Optimizer
from torchvision.transforms import Compose
from pytorch_lightning import LightningModule
import pytorch_lightning as pl

import consts
import data.pymunk_kvae
from data.pymunk_kvae import box, box_gravity, pong, polygon
from data.pymunk_kvae.pymunk_dataset import PymunkDataset
from data.transforms import time_first_collate_fn
from inference.smc.resampling import EffectiveSampleSizeResampleCriterion

from models_new_will_replace.base_rbpf_gls import BaseRBSMCGaussianLinearSystem
from models_new_will_replace.base_amortized_gls import BaseAmortizedGaussianLinearSystem
from models_new_will_replace.base_gls import Prediction, Latents

from visualization.plot_forecasts import make_val_plots_univariate
from experiments_new_will_replace.model_component_zoo.input_transforms import (
    InputTransformer,
)
from experiments_new_will_replace.model_component_zoo.input_transforms import (
    NoControlsDummyInputTransformer,
)
from experiments_new_will_replace.pymunk.evaluation import (
    compute_metrics,
    plot_pymunk_results,
)


# def remove_groundtruth(batch: dict):
#     keys_to_remove = ['position', 'velocity', 'y_gt']
#     return {k: v for k, v in batch.items() if k not in keys_to_remove}


class CastDtype(object):
    def __init__(self, model):
        self.model = model

    def __call__(self, item: dict) -> dict:
        return {
            k: v.to(self.model.dtype)
            if v.is_floating_point() else v
            for k, v in item.items()
        }


class PymunkExtractTarget(object):
    def __init__(
            self,
            past_length: int,
            prediction_length: Optional[int] = None,
    ):
        self.past_length = past_length
        self.prediction_length = prediction_length

    def __call__(self, item: dict) -> dict:
        transformed_item = {k: v for k, v in item.items() if k != "y"}
        target = item["y"]
        if self.prediction_length is not None:
            if not len(target) >= self.past_length + self.prediction_length:
                raise Exception(f"len(target)=={len(target)}, "
                                f"past_length=={self.past_length}, "
                                f"prediction_length=={self.prediction_length}")
            transformed_item['past_target'] = target[:self.past_length]
            transformed_item['future_target'] = \
                target[self.past_length:
                       self.past_length + self.prediction_length]
        else:
            transformed_item['past_target'] = target

        return transformed_item


class PymunkModel(LightningModule):
    def __init__(
        self,
        ssm: BaseAmortizedGaussianLinearSystem,
        dataset_name,
        lr,
        weight_decay,
        n_epochs,
        batch_sizes,
        past_length,
        n_particle_train,
        n_particle_eval,
        prediction_length,
        n_epochs_no_resampling=0,
        n_epochs_freeze_gls_params=0,
        num_batches_per_epoch=50,
        deterministic_forecast: bool = False,
    ):
        super().__init__()
        self.ctrl_transformer = NoControlsDummyInputTransformer()
        self.ssm = ssm
        self.past_length = past_length
        self.prediction_length = prediction_length
        self.batch_sizes = batch_sizes
        self.n_epochs_no_resampling = n_epochs_no_resampling
        self.n_epochs_freeze_gls_params = n_epochs_freeze_gls_params
        self.num_batches_per_epoch = num_batches_per_epoch

        self.lr = lr
        self.weight_decay = weight_decay
        self.n_epochs = n_epochs
        self.deterministic_forecast = deterministic_forecast

        assert n_particle_train == self.ssm.n_particle
        self._n_particle_train = n_particle_train
        self._n_particle_eval = n_particle_eval

        self.dataset_name = dataset_name

    def forward(
        self,
        past_target: torch.Tensor,
        feat_static_cat: Optional[torch.Tensor] = None,
        past_seasonal_indicators: Optional[torch.Tensor] = None,
        past_time_feat: Optional[torch.Tensor] = None,
        future_seasonal_indicators: Optional[torch.Tensor] = None,
        future_time_feat: Optional[torch.Tensor] = None,
        n_steps_forecast: int = 0,
        deterministic=False,
    ) -> (Sequence[Prediction], Sequence[Latents]):
        past_controls = self.ctrl_transformer(
            feat_static_cat=feat_static_cat,
            seasonal_indicators=past_seasonal_indicators,
            time_feat=past_time_feat,
        )
        future_controls = (
            self.ctrl_transformer(
                feat_static_cat=feat_static_cat,
                seasonal_indicators=future_seasonal_indicators,
                time_feat=future_time_feat,
            )
            if future_time_feat is not None
            else None
        )
        predictions_inferred, predictions_forecast = self.ssm.predict(
            n_steps_forecast=n_steps_forecast,
            past_targets=past_target,
            past_controls=past_controls,
            future_controls=future_controls,
            deterministic=deterministic,
        )
        return predictions_inferred, predictions_forecast

    def prepare_data(self):
        data_path = os.path.join(
            consts.data_dir, getattr(consts.Datasets, self.dataset_name),
        )
        if not os.path.exists(os.path.join(data_path, 'train.npz')):
            dataset_pkg = getattr(data.pymunk_kvae, self.dataset_name)
            dataset_pkg.generate_dataset()

    def train_dataloader(self):
        tar_extract_collate_fn = PymunkExtractTarget(
            past_length=self.past_length,
            prediction_length=None,
        )
        to_model_dtype = CastDtype(model=self)
        train_collate_fn = Compose(
            [time_first_collate_fn, tar_extract_collate_fn, to_model_dtype],
        )
        return DataLoader(
            dataset=PymunkDataset(
                file_path=os.path.join(
                    consts.data_dir, self.dataset_name, 'train.npz',
                ),
            ),
            batch_size=self.batch_sizes['train'],
            shuffle=True,
            num_workers=0,
            collate_fn=train_collate_fn,
        )

    def test_dataloader(self):
        tar_extract_collate_fn = PymunkExtractTarget(
            past_length=self.past_length,
            prediction_length=self.prediction_length,
        )
        to_model_dtype = CastDtype(model=self)
        test_collate_fn = Compose(
            [time_first_collate_fn, tar_extract_collate_fn, to_model_dtype],
        )
        return DataLoader(
            dataset=PymunkDataset(
                file_path=os.path.join(
                    consts.data_dir, self.dataset_name, 'test.npz',
                ),
            ),
            batch_size=self.batch_sizes['test'],
            shuffle=False,
            num_workers=0,
            collate_fn=test_collate_fn,
        )

    def optimizer_step(
            self,
            epoch: int,
            batch_idx: int,
            optimizer: Optimizer,
            optimizer_idx: int,
            *args,
            **kwargs,
    ) -> None:
        # warmup only certain parameters (all except GLS) if configured.
        is_warmup = (epoch < self.n_epochs_freeze_gls_params)
        if is_warmup:
            lr_gls = optimizer.param_groups[0]['lr']
            optimizer.param_groups[0]['lr'] = 0
            optimizer_output = super().optimizer_step(
                epoch, batch_idx, optimizer, optimizer_idx, *args, **kwargs,
            )
            optimizer.param_groups[0]['lr'] = lr_gls
        else:
            optimizer_output = super().optimizer_step(
                epoch, batch_idx, optimizer, optimizer_idx, *args, **kwargs,
            )
        return optimizer_output

    def configure_optimizers(self):
        param_names_gls = [
            name
            for name in dict(self.named_parameters()).keys()
            if
            ("gls_base_parameters" in name) and (not "link_transformers" in name)
        ]
        params_gls = tuple(
            param
            for name, param in self.named_parameters()
            if name in param_names_gls
        )
        params_except_gls = tuple(
            param
            for name, param in self.named_parameters()
            if name not in param_names_gls
        )
        assert len(params_except_gls) < len(tuple(self.parameters()))

        optimizer = Adam(
            params=[
                {"params": params_gls, "lr": self.lr},
                {"params": params_except_gls, "lr": self.lr},
            ],
            betas=(0.9, 0.95),
            amsgrad=False,
            weight_decay=self.weight_decay,
        )

        n_iter_lr_decay_one_oom = max(int(self.n_epochs / 2), 1)
        decay_rate = (1 / 10) ** (1 / n_iter_lr_decay_one_oom)
        scheduler = torch.optim.lr_scheduler.ExponentialLR(
            optimizer, gamma=decay_rate,
        )
        return [optimizer], [scheduler]

    def training_step(self, batch, batch_idx):
        loss = self.loss(**batch)
        result = pl.TrainResult(loss)
        result.log('train_loss', loss)
        return result

    def test_step(self, batch, batch_idx):
        # 1) Plot
        if batch_idx == 0:
            for deterministic in [True, False]:
                n_particle = self.ssm.n_particle
                if deterministic:
                    self.ssm.n_particle = 1
                plot_pymunk_results(
                    model=self,
                    batch=batch,
                    deterministic=deterministic,
                    plot_path=os.path.join(self.trainer.default_root_dir, "plot"),
                )
                self.ssm.n_particle = n_particle

        # 2) Compute metrics
        metrics = compute_metrics(model=self, batch=batch)
        result = pl.EvalResult()
        for k, v in metrics.items():
            result.log(k, v)
        return result

    def test_end(self, outputs):
        result = pl.EvalResult()
        keys = outputs[0].keys()
        agg_metrics = {}
        for k in keys:
            # TODO: Check that the aggr. dimensions are also correct for accuracy
            metrics_cat = np.concatenate([output[k] for output in outputs])
            agg_metrics["mean"] = metrics_cat.mean(axis=1).mean(axis=-1)
            agg_metrics["std"] = metrics_cat.std(axis=1).mean(axis=-1)
            agg_metrics["var"] = metrics_cat.var(axis=1).mean(axis=-1)
            for which_agg, agg_metric in agg_metrics.items():
                result.log(f"{k}_{which_agg}", agg_metric)

    def on_train_start(self) -> None:
        os.makedirs(
            os.path.join(self.trainer.default_root_dir, "plot"),
            exist_ok=True,
        )

    def on_train_epoch_start(self) -> None:
        super().on_train_epoch_start()
        self.ssm.n_particle = self._n_particle_train

        # Set to no re-sampling if configured.
        # Note that this will omit the very first iteration
        # since the computation happens before this function,
        # but it should not be a problem. Want to get rid of this anyways.
        if isinstance(self.ssm, BaseRBSMCGaussianLinearSystem):
            # <= and >= because we want to set self._resampling_criterion_fn.
            set_no_resampling = \
                (self.current_epoch <= self.n_epochs_no_resampling)
            set_resampling = \
                (self.current_epoch >= self.n_epochs_no_resampling)

            if set_no_resampling:
                self._resampling_criterion_fn = self.ssm.resampling_criterion_fn
                self.ssm.resampling_criterion_fn = \
                    EffectiveSampleSizeResampleCriterion(
                        min_ess_ratio=0.0,
                    )
            if set_resampling:
                self.ssm.resampling_criterion_fn = self._resampling_criterion_fn

    def on_test_epoch_start(self) -> None:
        super().on_test_epoch_start()
        self.ssm.n_particle = self._n_particle_eval

    def loss(
        self,
        past_target: torch.Tensor,
        feat_static_cat: Optional[torch.Tensor] = None,
        past_seasonal_indicators: Optional[torch.Tensor] = None,
        past_time_feat: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        T, B = past_target.shape[:2]

        past_controls = self.ctrl_transformer(
            feat_static_cat=feat_static_cat,
            seasonal_indicators=past_seasonal_indicators,
            time_feat=past_time_feat,
        )
        loss_samplewise = self.ssm.loss(
            past_targets=past_target,
            past_controls=past_controls,
            **{k: v for k, v in kwargs.items() if not "future" in k},
        )
        loss = loss_samplewise.sum(dim=0) / (T * B)
        return loss

