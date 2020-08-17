import os

from torch.utils.data import DataLoader
from torchvision.transforms import Compose
import pytorch_lightning as pl
import matplotlib.pyplot as plt
import numpy as np

import consts
from experiments.default_lightning_model import (
    DefaultLightningModel,
    DefaultExtractTarget,
)
import data.pymunk_kvae
from data.pymunk_kvae.pymunk_dataset import PymunkDataset
from data.transforms import time_first_collate_fn
from experiments.pymunk.evaluation import (
    compute_metrics,
    plot_pymunk_results,
)


class CastDtype(object):
    def __init__(self, model):
        self.model = model

    def __call__(self, item: dict) -> dict:
        return {
            k: v.to(self.model.dtype)
            if v.is_floating_point() else v
            for k, v in item.items()
        }


class PymunkModel(DefaultLightningModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_metrics = {}

    def prepare_data(self):
        data_path = os.path.join(
            consts.data_dir, getattr(consts.Datasets, self.dataset_name),
        )
        if not os.path.exists(os.path.join(data_path, 'train.npz')):
            dataset_pkg = getattr(data.pymunk_kvae, self.dataset_name)
            dataset_pkg.generate_dataset()

    def train_dataloader(self):
        tar_extract_collate_fn = DefaultExtractTarget(
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
            num_workers=8,
            collate_fn=train_collate_fn,
        )

    def test_dataloader(self):
        tar_extract_collate_fn = DefaultExtractTarget(
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
            num_workers=8,
            collate_fn=test_collate_fn,
        )

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
                    plot_path=os.path.join(self.logger.log_dir, "plots"),
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
        agg_metrics = {}
        metric_names = [k for k in outputs.keys() if k != 'meta']
        for metric_name in metric_names:
            metrics_cat = np.concatenate(outputs[metric_name], axis=-1)
            assert metrics_cat.shape[:-1] == (
                self.past_length + self.prediction_length, self.ssm.n_particle,)
            # mean, std, var over particle dim. Always mean over batch/data.
            agg_metrics["mean"] = metrics_cat.mean(axis=1).mean(axis=-1)
            agg_metrics["std"] = metrics_cat.std(axis=1).mean(axis=-1)
            agg_metrics["var"] = metrics_cat.var(axis=1).mean(axis=-1)
            # TODO: currently there is no way to log non-scalar metrics in
            #  pytorch-lightning? This is a temporary hack to make the metric
            #  over time (vector) available to outside for plotting.
            #  But this should abolutely be a feature of lightning...
            for which_agg, agg_metric in agg_metrics.items():
                full_metric_name = f"{metric_name}_{which_agg}"
                self.log_metrics[full_metric_name] = agg_metric
                result.log(full_metric_name, agg_metric.mean(axis=0))

        # There are no methods in lightning or even tensorboard to log
        # 1D tensors for a standard line-plot. So we save them with
        # numpy instead and make a standard matplotlib plot.
        np.savez(
            os.path.join(
                self.logger.log_dir,
                "metrics",
                "sequence_metrics.npz",
            ),
            self.log_metrics,
        )

        time = np.arange(self.past_length + self.prediction_length)
        for metric_name in metric_names:
            m = self.log_metrics[f"{metric_name}_mean"]
            std = self.log_metrics[f"{metric_name}_std"]
            fig = plt.figure()
            plt.plot(time, m, label="mean")
            plt.fill_between(
                time, m - 3 * std, m + 3 * std, alpha=0.25, label="3 std",
            )
            plt.axvline(
                self.past_length - 1,
                linestyle="--",
                color="black",
                label='_nolegend_',
            )
            plt.legend()
            plt.xlabel("t")
            plt.ylabel(metric_name)
            plt.savefig(
                os.path.join(
                    self.logger.log_dir, "plots", f"{metric_name}.pdf",
                ),
                bbox_inches="tight",
                pad_inches=0.025,
            )
            plt.close(fig)

        return result

