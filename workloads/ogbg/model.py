import torch
from workloads import WorkloadModel
from runtime.specs import RuntimeSpecs
from submissions import Submission
from torch_geometric.nn import GAT
from torchmetrics import Accuracy
from torch_geometric.nn import GAT, GIN, MLP, global_add_pool
from ogb.graphproppred import Evaluator


class OGBGModel(WorkloadModel):
    """GIN from pytorch geometric"""
    def __init__(self, submission: Submission, node_feature_dim: int, num_classes: int, dataset_name: str):
        # https://github.com/pyg-team/pytorch_geometric/blob/master/examples/pytorch_lightning/gin.py
        in_channels: int = node_feature_dim
        out_channels: int = num_classes
        hidden_channels: int = 64
        num_layers: int = 3
        dropout: float = 0.5
        model = GIN(
            in_channels = in_channels,
            hidden_channels = hidden_channels,
            num_layers=num_layers,
            dropout=dropout,
            jk='cat')
        super().__init__(model, submission)
        
        self.classifier = MLP(
            [hidden_channels, hidden_channels, out_channels],
            norm="batch_norm",
            dropout=dropout)
        self.train_acc = Accuracy(task='multiclass', num_classes=out_channels)
        self.val_acc = Accuracy(task='multiclass', num_classes=out_channels)
        self.test_acc = Accuracy(task='multiclass', num_classes=out_channels)

        # https://ogb.stanford.edu/docs/home/
        self.evaluator = Evaluator(name=dataset_name)
        # You can learn the input and output format specification of the evaluator as follows.
        # print(evaluator.expected_input_format) 
        # print(evaluator.expected_output_format) 
        # input_dict = {"y_true": y_true, "y_pred": y_pred}
        # result_dict = evaluator.eval(input_dict) # E.g., {"rocauc": 0.7321} 

        self.loss_fn = torch.nn.CrossEntropyLoss()


    def forward(self, x, edge_index, batch) -> torch.Tensor:
        x = self.model(x, edge_index)
        x = global_add_pool(x, batch)
        x = self.classifier(x)
        return x

    def training_step(self, data, batch_idx):
        y_hat = self(data.x, data.edge_index, data.batch)
        loss = self.loss_fn(y_hat, data.y)
        self.train_acc(y_hat.softmax(dim=-1), data.y)
        self.log('train_acc', self.train_acc, prog_bar=True, on_step=False, on_epoch=True)
        return loss

    def validation_step(self, data, batch_idx):
        y_hat = self(data.x, data.edge_index, data.batch)
        self.val_acc(y_hat.softmax(dim=-1), data.y)
        self.log('val_acc', self.val_acc, prog_bar=True, on_step=False, on_epoch=True)

    def test_step(self, data, batch_idx):
        y_hat = self(data.x, data.edge_index, data.batch)
        self.test_acc(y_hat.softmax(dim=-1), data.y)
        self.log('test_acc', self.test_acc, prog_bar=True, on_step=False, on_epoch=True)

    def get_specs(self) -> RuntimeSpecs:
        # TODO have another look at epochs etc
        return RuntimeSpecs(
            max_epochs=50,
            max_steps=None,
            devices=1,
            target_metric="val_acc",
            target_metric_mode="max"
        )
