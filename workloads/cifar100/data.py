import torch
from torchvision.datasets import CIFAR100
from torchvision.transforms import v2
from workloads import WorkloadDataModule
from runtime.configs import WorkloadConfig


class CIFAR100DataModule(WorkloadDataModule):
    def __init__(self, workload_config: WorkloadConfig):
        # cifar 100 has 60000 32x32 color images (600 images per class)
        super().__init__(workload_config)
        cifar100_mean = (0.4914, 0.4822, 0.4465)
        cifar100_stddev = (0.2023, 0.1994, 0.2010)

        # build the transforms as given in config
        random_crop = v2.RandomCrop(
            size = workload_config.train_transforms["random_crop"]["size"],
            padding = workload_config.train_transforms["random_crop"]["padding"],
            padding_mode = workload_config.train_transforms["random_crop"]["padding_mode"],
        )
        horizontal_flip = v2.RandomHorizontalFlip(
            workload_config.train_transforms["horizontal_flip"]["p"]
        )
        trivial_augment = v2.TrivialAugmentWide(interpolation=v2.InterpolationMode.BILINEAR)

        # build list and remove unwanted before composing
        train_transforms = [
            v2.ToImage(),
            random_crop,
            horizontal_flip,
            trivial_augment,
            v2.ToDtype(torch.float, scale=True),
            v2.Normalize(cifar100_mean, cifar100_stddev),
            v2.ToPureTensor()
        ]

        if not workload_config.train_transforms["random_crop"]["use"]:
            train_transforms.remove(random_crop)
        if not workload_config.train_transforms["horizontal_flip"]["use"]:
            train_transforms.remove(horizontal_flip)
        if not workload_config.train_transforms["trivial_augment"]["use"]:
            train_transforms.remove(trivial_augment)

        self.train_transforms = v2.Compose(train_transforms)
        self.val_transforms = v2.Compose([
            v2.ToImage(),
            v2.ToDtype(torch.float, scale=True),
            v2.Normalize(cifar100_mean, cifar100_stddev),
            v2.ToPureTensor()
        ])

    def prepare_data(self):
        # download
        CIFAR100(str(self.data_dir), train=True, download=True)
        CIFAR100(str(self.data_dir), train=False, download=True)

    def setup(self, stage: str):
        """setup is called from every process across all the nodes. Setting state here is recommended.
        """
        if stage == "fit":
            self.data_train = self._get_dataset(train=True)
            self.data_val = self._get_dataset(train=False)

        if stage == "validate":
            self.data_val = self._get_dataset(train=False)

        if stage == "test":
            self.data_test = self._get_dataset(train=False)

        if stage == "predict":
            self.data_predict = self._get_dataset(train=False)

    def _get_dataset(self, train: bool):
        if train:
            return CIFAR100(str(self.data_dir), train=True, transform=self.train_transforms)
        else:
            return CIFAR100(str(self.data_dir), train=False, transform=self.val_transforms)
