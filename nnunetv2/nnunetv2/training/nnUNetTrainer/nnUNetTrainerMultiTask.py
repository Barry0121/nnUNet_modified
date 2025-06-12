from cProfile import label
import torch
from torch import nn
from torch import distributed as dist
import os
import numpy as np
import pandas as pd
from typing import Union, Tuple, List
from nnunetv2.training.nnUNetTrainer.variants.network_architecture.nnUNetTrainerNoDeepSupervision import nnUNetTrainerNoDeepSupervision
from nnunetv2.architectures.MultiTaskResEncUNet import MultiTaskResEncUNet, MultiTaskChannelAttentionResEncUNet, MultiTaskEfficientAttentionResEncUNet
from nnunetv2.training.loss.multitask_losses import MultiTaskLoss
from nnunetv2.training.dataloading.nnunet_dataset import infer_dataset_class
from nnunetv2.training.loss.dice import get_tp_fp_fn_tn
from nnunetv2.utilities.collate_outputs import collate_outputs


class nnUNetTrainerMultiTask(nnUNetTrainerNoDeepSupervision):
    """Multi-task trainer for segmentation + classification"""

    def __init__(self, plans: dict, configuration: str, fold: int, dataset_json: dict,
                 device: torch.device = torch.device('cuda')):
        super().__init__(plans, configuration, fold, dataset_json, device)

        # Multi-task specific parameters
        self.num_classification_classes = 3  # Update based on your subtypes
        self.seg_weight = 1.0
        self.cls_weight = 0.5
        self.loss_type = 'dice_ce'  # Options: 'dice_ce', 'focal', 'tversky'


    def get_tr_and_val_datasets(self):
        """Override to use dataset class with classification labels"""
        tr_keys, val_keys = self.do_split()
        dataset_name = self.plans_manager.dataset_name

        # Infer the appropriate dataset class (numpy or blosc2)
        dataset_class = infer_dataset_class(self.preprocessed_dataset_folder)

        # Use dataset class with classification labels enabled
        dataset_tr = dataset_class(
            self.preprocessed_dataset_folder, tr_keys,
            folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage,
            load_subtype_labels=True,
            label_path=os.path.join(os.environ['nnUNet_raw'], dataset_name, "labels.csv")
        )
        dataset_val = dataset_class(
            self.preprocessed_dataset_folder, val_keys,
            folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage,
            load_subtype_labels=True,
            label_path=os.path.join(os.environ['nnUNet_raw'], dataset_name, "labels.csv")
        )

        return dataset_tr, dataset_val

    @staticmethod
    def build_network_architecture(architecture_class_name: str,
                                   arch_init_kwargs: dict,
                                   arch_init_kwargs_req_import: Union[List[str], Tuple[str, ...]],
                                   num_input_channels: int,
                                   num_output_channels: int,
                                   enable_deep_supervision: bool = True) -> nn.Module:
        """
        Build the multi-task network architecture.
        This method follows the nnUNetv2 trainer interface but builds our custom multi-task network.
        """

        # Handle the import requirements for architecture kwargs
        import pydoc
        architecture_kwargs = dict(**arch_init_kwargs)
        for ri in arch_init_kwargs_req_import:
            if architecture_kwargs[ri] is not None:
                architecture_kwargs[ri] = pydoc.locate(architecture_kwargs[ri])

        # Map architecture class names to our custom classes
        architecture_mapping = {
            'nnunetv2.architectures.MultiTaskResEncUNet.MultiTaskResEncUNet': MultiTaskResEncUNet,
            'nnunetv2.architectures.MultiTaskResEncUNet.MultiTaskChannelAttentionResEncUNet': MultiTaskChannelAttentionResEncUNet,
            'nnunetv2.architectures.MultiTaskResEncUNet.MultiTaskEfficientAttentionResEncUNet': MultiTaskEfficientAttentionResEncUNet,
            # Add fallback for just the class name
            'MultiTaskResEncUNet': MultiTaskResEncUNet,
            'MultiTaskChannelAttentionResEncUNet': MultiTaskChannelAttentionResEncUNet,
            'MultiTaskEfficientAttentionResEncUNet': MultiTaskEfficientAttentionResEncUNet,
        }

        # Get the network class
        if architecture_class_name in architecture_mapping:
            network_class = architecture_mapping[architecture_class_name]
        else:
            # Fallback to default nnUNet behavior
            raise ValueError(f"Unknown architecture_class_name: {architecture_class_name}")

        # Create the network - note the different parameter names for multi-task networks
        network = network_class(
            input_channels=num_input_channels,
            num_classes=num_output_channels,
            # num_classification_classes=3,  # Update based on your classification classes
            **architecture_kwargs
        )

        # Initialize the network if it has an initialize method
        if hasattr(network, 'initialize'):
            network.apply(network.initialize)

        return network

    def _build_loss(self):
        """Override to use multi-task loss"""
        return MultiTaskLoss(
            seg_weight=self.seg_weight,
            cls_weight=self.cls_weight,
            loss_type=self.loss_type
        )

    def train_step(self, batch: dict) -> dict:
        """
        Custom training step for multi-task learning.
        """
        data = batch['data'].to(self.device)
        target_seg = batch['target'].to(self.device)  # Segmentation targets
        target_cls = batch['class_target'].to(self.device)  # Classification targets

        self.optimizer.zero_grad()

        # Forward pass
        output = self.network(data)

        # Multi-task output: segmentation and classification
        if isinstance(output, dict) and len(output) == 2:
            seg_output, cls_output = (output['segmentation'], output['classification'])
        else:
            # Fallback if network returns only segmentation
            seg_output = output
            cls_output = None

        # Calculate loss
        loss_dict = self.loss(seg_output, target_seg, cls_output, target_cls)

        # Backward pass
        loss_dict['loss'].backward()
        self.optimizer.step()

        return loss_dict

    def validation_step(self, batch: dict) -> dict:
        """
        Custom validation step for multi-task learning.
        Calculates both segmentation and classification metrics.
        """
        data = batch['data'].to(self.device)
        target_seg = batch['target'].to(self.device)  # Segmentation targets
        target_cls = batch['class_target'].to(self.device)  # Classification targets

        with torch.no_grad():
            output = self.network(data)

            # Multi-task output
            if isinstance(output, dict) and len(output) == 2:
                seg_output, cls_output = (output['segmentation'], output['classification'])
            else:
                seg_output = output
                cls_output = None

            # Calculate validation loss
            loss_dict = self.loss(seg_output, target_seg, cls_output, target_cls)

            # === SEGMENTATION METRICS ===
            # Handle deep supervision if enabled
            if self.enable_deep_supervision:
                seg_output_for_metrics = seg_output[0]  # Use highest resolution
                target_seg_for_metrics = target_seg[0] if isinstance(target_seg, list) else target_seg
            else:
                seg_output_for_metrics = seg_output
                target_seg_for_metrics = target_seg

            # Generate segmentation predictions
            axes = [0] + list(range(2, seg_output_for_metrics.ndim))

            if self.label_manager.has_regions:
                predicted_segmentation_onehot = (torch.sigmoid(seg_output_for_metrics) > 0.5).long()
            else:
                # Standard multi-class segmentation
                output_seg = seg_output_for_metrics.argmax(1)[:, None]
                predicted_segmentation_onehot = torch.zeros(
                    seg_output_for_metrics.shape,
                    device=seg_output_for_metrics.device,
                    dtype=torch.float32
                )
                predicted_segmentation_onehot.scatter_(1, output_seg, 1)
                del output_seg

            # Handle ignore labels if present
            if self.label_manager.has_ignore_label:
                if not self.label_manager.has_regions:
                    mask = (target_seg_for_metrics != self.label_manager.ignore_label).float()
                    target_seg_for_metrics = target_seg_for_metrics.clone()
                    target_seg_for_metrics[target_seg_for_metrics == self.label_manager.ignore_label] = 0
                else:
                    if target_seg_for_metrics.dtype == torch.bool:
                        mask = ~target_seg_for_metrics[:, -1:]
                    else:
                        mask = 1 - target_seg_for_metrics[:, -1:]
                    target_seg_for_metrics = target_seg_for_metrics[:, :-1]
            else:
                mask = None

            # Calculate TP, FP, FN for segmentation
            tp, fp, fn, _ = get_tp_fp_fn_tn(
                predicted_segmentation_onehot,
                target_seg_for_metrics,
                axes=axes,
                mask=mask
            )

            tp_hard = tp.detach().cpu().numpy()
            fp_hard = fp.detach().cpu().numpy()
            fn_hard = fn.detach().cpu().numpy()

            # Remove background class for standard segmentation
            if not self.label_manager.has_regions:
                tp_hard = tp_hard[1:]  # Remove background
                fp_hard = fp_hard[1:]
                fn_hard = fn_hard[1:]

            # === CLASSIFICATION METRICS ===
            cls_metrics = {}
            if cls_output is not None and target_cls is not None:
                # Get predicted classes
                cls_pred = torch.argmax(cls_output, dim=1)  # Shape: [batch_size]
                cls_target = target_cls.long()  # Ensure target is long type

                # Calculate per-class metrics
                num_classes = cls_output.shape[1]
                cls_tp = torch.zeros(num_classes, dtype=torch.long)
                cls_fp = torch.zeros(num_classes, dtype=torch.long)
                cls_fn = torch.zeros(num_classes, dtype=torch.long)
                cls_tn = torch.zeros(num_classes, dtype=torch.long)

                for class_idx in range(num_classes):
                    # Binary classification metrics for each class
                    pred_positive = (cls_pred == class_idx)
                    target_positive = (cls_target == class_idx)

                    cls_tp[class_idx] = (pred_positive & target_positive).sum()
                    cls_fp[class_idx] = (pred_positive & ~target_positive).sum()
                    cls_fn[class_idx] = (~pred_positive & target_positive).sum()
                    cls_tn[class_idx] = (~pred_positive & ~target_positive).sum()

                cls_metrics = {
                    'cls_tp': cls_tp.cpu().numpy(),
                    'cls_fp': cls_fp.cpu().numpy(),
                    'cls_fn': cls_fn.cpu().numpy(),
                    'cls_tn': cls_tn.cpu().numpy(),
                    'cls_correct': (cls_pred == cls_target).sum().cpu().numpy(),
                    'cls_total': cls_target.numel()
                }

            # Combine all metrics
            result = {
                'loss': loss_dict.get('total_loss', 0.0).detach().cpu().numpy() if isinstance(loss_dict.get('total_loss', 0.0), torch.Tensor) else loss_dict.get('total_loss', 0.0),
                'seg_loss': loss_dict.get('seg_loss', 0.0).detach().cpu().numpy() if isinstance(loss_dict.get('seg_loss', 0.0), torch.Tensor) else loss_dict.get('seg_loss', 0.0),
                'cls_loss': loss_dict.get('cls_loss', 0.0).detach().cpu().numpy() if isinstance(loss_dict.get('cls_loss', 0.0), torch.Tensor) else loss_dict.get('cls_loss', 0.0),
                'tp_hard': tp_hard,
                'fp_hard': fp_hard,
                'fn_hard': fn_hard,
                **cls_metrics
            }
            import pdb
            pdb.set_trace()
            return result

    def on_validation_epoch_end(self, val_outputs: List[dict]):
        """
        Custom validation epoch end for multi-task learning.
        Calculates and logs both segmentation and classification metrics.
        """
        outputs_collated = collate_outputs(val_outputs)

        import pdb
        pdb.set_trace()

        # === SEGMENTATION METRICS ===
        tp = np.sum(outputs_collated['tp_hard'], 0)
        fp = np.sum(outputs_collated['fp_hard'], 0)
        fn = np.sum(outputs_collated['fn_hard'], 0)

        # Handle distributed training for segmentation metrics
        if self.is_ddp:
            world_size = dist.get_world_size()

            # Gather segmentation metrics
            tps = [None for _ in range(world_size)]
            dist.all_gather_object(tps, tp)
            tp = np.vstack([i[None] for i in tps]).sum(0)

            fps = [None for _ in range(world_size)]
            dist.all_gather_object(fps, fp)
            fp = np.vstack([i[None] for i in fps]).sum(0)

            fns = [None for _ in range(world_size)]
            dist.all_gather_object(fns, fn)
            fn = np.vstack([i[None] for i in fns]).sum(0)

            # Gather losses
            losses_val = [None for _ in range(world_size)]
            dist.all_gather_object(losses_val, outputs_collated['loss'])
            total_loss = np.vstack(losses_val).mean()

            seg_losses_val = [None for _ in range(world_size)]
            dist.all_gather_object(seg_losses_val, outputs_collated['seg_loss'])
            seg_loss = np.vstack(seg_losses_val).mean()

            cls_losses_val = [None for _ in range(world_size)]
            dist.all_gather_object(cls_losses_val, outputs_collated['cls_loss'])
            cls_loss = np.vstack(cls_losses_val).mean()
        else:
            total_loss = np.mean(outputs_collated['loss'])
            seg_loss = np.mean(outputs_collated['seg_loss'])
            cls_loss = np.mean(outputs_collated['cls_loss'])

        # Calculate segmentation Dice scores
        global_dc_per_class = [2 * i / (2 * i + j + k) for i, j, k in zip(tp, fp, fn)]
        mean_fg_dice = np.nanmean(global_dc_per_class)

        # === CLASSIFICATION METRICS ===
        cls_metrics_summary = {}
        if 'cls_tp' in outputs_collated and outputs_collated['cls_tp'].size > 0:
            # Aggregate classification metrics
            cls_tp = np.sum(outputs_collated['cls_tp'], 0)
            cls_fp = np.sum(outputs_collated['cls_fp'], 0)
            cls_fn = np.sum(outputs_collated['cls_fn'], 0)
            cls_tn = np.sum(outputs_collated['cls_tn'], 0)
            cls_correct = np.sum(outputs_collated['cls_correct'])
            cls_total = np.sum(outputs_collated['cls_total'])

            # Handle distributed training for classification metrics
            if self.is_ddp:
                # Gather classification metrics
                cls_tps = [None for _ in range(world_size)]
                dist.all_gather_object(cls_tps, cls_tp)
                cls_tp = np.vstack([i[None] for i in cls_tps]).sum(0)

                cls_fps = [None for _ in range(world_size)]
                dist.all_gather_object(cls_fps, cls_fp)
                cls_fp = np.vstack([i[None] for i in cls_fps]).sum(0)

                cls_fns = [None for _ in range(world_size)]
                dist.all_gather_object(cls_fns, cls_fn)
                cls_fn = np.vstack([i[None] for i in cls_fns]).sum(0)

                cls_corrects = [None for _ in range(world_size)]
                dist.all_gather_object(cls_corrects, cls_correct)
                cls_correct = np.sum(cls_corrects)

                cls_totals = [None for _ in range(world_size)]
                dist.all_gather_object(cls_totals, cls_total)
                cls_total = np.sum(cls_totals)

            # Calculate classification metrics
            cls_accuracy = cls_correct / cls_total if cls_total > 0 else 0.0

            # Per-class precision, recall, F1
            cls_precision = np.divide(cls_tp, cls_tp + cls_fp, out=np.zeros_like(cls_tp, dtype=float), where=(cls_tp + cls_fp) != 0)
            cls_recall = np.divide(cls_tp, cls_tp + cls_fn, out=np.zeros_like(cls_tp, dtype=float), where=(cls_tp + cls_fn) != 0)
            cls_f1 = np.divide(2 * cls_precision * cls_recall, cls_precision + cls_recall,
                            out=np.zeros_like(cls_precision), where=(cls_precision + cls_recall) != 0)

            # Macro averages
            macro_precision = np.nanmean(cls_precision)
            macro_recall = np.nanmean(cls_recall)
            macro_f1 = np.nanmean(cls_f1)

            cls_metrics_summary = {
                'cls_accuracy': cls_accuracy,
                'cls_precision_per_class': cls_precision,
                'cls_recall_per_class': cls_recall,
                'cls_f1_per_class': cls_f1,
                'cls_macro_precision': macro_precision,
                'cls_macro_recall': macro_recall,
                'cls_macro_f1': macro_f1
            }

        # === LOGGING ===
        # Log segmentation metrics
        self.logger.log('mean_fg_dice', mean_fg_dice, self.current_epoch)
        self.logger.log('dice_per_class_or_region', global_dc_per_class, self.current_epoch)
        self.logger.log('val_losses', total_loss, self.current_epoch)

        # Log losses
        self.logger.log('val_total_loss', total_loss, self.current_epoch)
        self.logger.log('val_seg_loss', seg_loss, self.current_epoch)
        self.logger.log('val_cls_loss', cls_loss, self.current_epoch)

        # Log classification metrics
        if cls_metrics_summary:
            self.logger.log('cls_accuracy', cls_metrics_summary['cls_accuracy'], self.current_epoch)
            self.logger.log('cls_macro_f1', cls_metrics_summary['cls_macro_f1'], self.current_epoch)
            self.logger.log('cls_macro_precision', cls_metrics_summary['cls_macro_precision'], self.current_epoch)
            self.logger.log('cls_macro_recall', cls_metrics_summary['cls_macro_recall'], self.current_epoch)
            self.logger.log('cls_f1_per_class', cls_metrics_summary['cls_f1_per_class'], self.current_epoch)
            self.logger.log('cls_precision_per_class', cls_metrics_summary['cls_precision_per_class'], self.current_epoch)
            self.logger.log('cls_recall_per_class', cls_metrics_summary['cls_recall_per_class'], self.current_epoch)

        # === CONSOLE OUTPUT ===
        print(f"\n=== Validation Epoch {self.current_epoch} Results ===")
        print(f"Total Loss: {total_loss:.4f}")
        print(f"Segmentation Loss: {seg_loss:.4f}")
        print(f"Classification Loss: {cls_loss:.4f}")
        print(f"Mean Foreground Dice: {mean_fg_dice:.4f}")

        if len(global_dc_per_class) >= 2:
            print(f"Pancreas Dice: {global_dc_per_class[0]:.4f}")
            print(f"Lesion Dice: {global_dc_per_class[1]:.4f}")

        if cls_metrics_summary:
            print(f"Classification Accuracy: {cls_metrics_summary['cls_accuracy']:.4f}")
            print(f"Classification Macro F1: {cls_metrics_summary['cls_macro_f1']:.4f}")
            print(f"Classification Macro Precision: {cls_metrics_summary['cls_macro_precision']:.4f}")
            print(f"Classification Macro Recall: {cls_metrics_summary['cls_macro_recall']:.4f}")

        print("=" * 50)

        return {
            'mean_fg_dice': mean_fg_dice,
            'dice_per_class': global_dc_per_class,
            'total_loss': total_loss,
            'seg_loss': seg_loss,
            'cls_loss': cls_loss,
            **cls_metrics_summary
        }


    def on_train_epoch_start(self):
        """
        Hook called at the start of each training epoch.
        Can be used for custom logic like dynamic loss weighting.
        """
        super().on_train_epoch_start()

        # Example: Dynamic loss weighting based on epoch
        if hasattr(self, 'current_epoch'):
            # Gradually increase classification weight
            epoch_ratio = min(self.current_epoch / 100.0, 1.0)  # Reach max at epoch 100
            self.loss.cls_weight = self.cls_weight * epoch_ratio

    def run_training(self):
        """
        Override the main training loop if needed for multi-task specific logic.
        """
        # You can add custom training logic here
        # For now, use the parent implementation
        super().run_training()
