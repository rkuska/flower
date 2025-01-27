# Copyright 2020 Flower Labs GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contextmanager for a gRPC streaming channel to the Flower server."""


import uuid
from contextlib import contextmanager
from logging import DEBUG
from pathlib import Path
from queue import Queue
from typing import Callable, Iterator, Optional, Tuple, Union, cast

from flwr.common import GRPC_MAX_MESSAGE_LENGTH
from flwr.common import recordset_compat as compat
from flwr.common import serde
from flwr.common.configsrecord import ConfigsRecord
from flwr.common.constant import (
    TASK_TYPE_EVALUATE,
    TASK_TYPE_FIT,
    TASK_TYPE_GET_PARAMETERS,
    TASK_TYPE_GET_PROPERTIES,
)
from flwr.common.grpc import create_channel
from flwr.common.logger import log
from flwr.common.recordset import RecordSet
from flwr.proto.node_pb2 import Node  # pylint: disable=E0611
from flwr.proto.task_pb2 import Task, TaskIns, TaskRes  # pylint: disable=E0611
from flwr.proto.transport_pb2 import (  # pylint: disable=E0611
    ClientMessage,
    Reason,
    ServerMessage,
)
from flwr.proto.transport_pb2_grpc import FlowerServiceStub  # pylint: disable=E0611

# The following flags can be uncommented for debugging. Other possible values:
# https://github.com/grpc/grpc/blob/master/doc/environment_variables.md
# import os
# os.environ["GRPC_VERBOSITY"] = "debug"
# os.environ["GRPC_TRACE"] = "tcp,http"


def on_channel_state_change(channel_connectivity: str) -> None:
    """Log channel connectivity."""
    log(DEBUG, channel_connectivity)


@contextmanager
def grpc_connection(  # pylint: disable=R0915
    server_address: str,
    insecure: bool,
    max_message_length: int = GRPC_MAX_MESSAGE_LENGTH,
    root_certificates: Optional[Union[bytes, str]] = None,
) -> Iterator[
    Tuple[
        Callable[[], Optional[TaskIns]],
        Callable[[TaskRes], None],
        Optional[Callable[[], None]],
        Optional[Callable[[], None]],
    ]
]:
    """Establish a gRPC connection to a gRPC server.

    Parameters
    ----------
    server_address : str
        The IPv4 or IPv6 address of the server. If the Flower server runs on the same
        machine on port 8080, then `server_address` would be `"0.0.0.0:8080"` or
        `"[::]:8080"`.
    max_message_length : int
        The maximum length of gRPC messages that can be exchanged with the Flower
        server. The default should be sufficient for most models. Users who train
        very large models might need to increase this value. Note that the Flower
        server needs to be started with the same value
        (see `flwr.server.start_server`), otherwise it will not know about the
        increased limit and block larger messages.
        (default: 536_870_912, this equals 512MB)
    root_certificates : Optional[bytes] (default: None)
        The PEM-encoded root certificates as a byte string or a path string.
        If provided, a secure connection using the certificates will be
        established to an SSL-enabled Flower server.

    Returns
    -------
    receive, send : Callable, Callable

    Examples
    --------
    Establishing a SSL-enabled connection to the server:

    >>> from pathlib import Path
    >>> with grpc_connection(
    >>>     server_address,
    >>>     max_message_length=max_message_length,
    >>>     root_certificates=Path("/crts/root.pem").read_bytes(),
    >>> ) as conn:
    >>>     receive, send = conn
    >>>     server_message = receive()
    >>>     # do something here
    >>>     send(client_message)
    """
    if isinstance(root_certificates, str):
        root_certificates = Path(root_certificates).read_bytes()

    channel = create_channel(
        server_address=server_address,
        insecure=insecure,
        root_certificates=root_certificates,
        max_message_length=max_message_length,
    )
    channel.subscribe(on_channel_state_change)

    queue: Queue[ClientMessage] = Queue(  # pylint: disable=unsubscriptable-object
        maxsize=1
    )
    stub = FlowerServiceStub(channel)

    server_message_iterator: Iterator[ServerMessage] = stub.Join(iter(queue.get, None))

    def receive() -> TaskIns:
        # Receive ServerMessage proto
        proto = next(server_message_iterator)

        # ServerMessage proto --> *Ins --> RecordSet
        field = proto.WhichOneof("msg")
        task_type = ""
        if field == "get_properties_ins":
            recordset = compat.getpropertiesins_to_recordset(
                serde.get_properties_ins_from_proto(proto.get_properties_ins)
            )
            task_type = TASK_TYPE_GET_PROPERTIES
        elif field == "get_parameters_ins":
            recordset = compat.getparametersins_to_recordset(
                serde.get_parameters_ins_from_proto(proto.get_parameters_ins)
            )
            task_type = TASK_TYPE_GET_PARAMETERS
        elif field == "fit_ins":
            recordset = compat.fitins_to_recordset(
                serde.fit_ins_from_proto(proto.fit_ins), False
            )
            task_type = TASK_TYPE_FIT
        elif field == "evaluate_ins":
            recordset = compat.evaluateins_to_recordset(
                serde.evaluate_ins_from_proto(proto.evaluate_ins), False
            )
            task_type = TASK_TYPE_EVALUATE
        elif field == "reconnect_ins":
            recordset = RecordSet()
            recordset.set_configs(
                "config", ConfigsRecord({"seconds": proto.reconnect_ins.seconds})
            )
            task_type = "reconnect"
        else:
            raise ValueError(
                "Unsupported instruction in ServerMessage, "
                "cannot deserialize from ProtoBuf"
            )

        # RecordSet --> RecordSet proto
        recordset_proto = serde.recordset_to_proto(recordset)

        # Construct TaskIns
        return TaskIns(
            task_id=str(uuid.uuid4()),
            group_id="",
            run_id=0,
            task=Task(
                producer=Node(node_id=0, anonymous=True),
                consumer=Node(node_id=0, anonymous=True),
                ancestry=[],
                task_type=task_type,
                recordset=recordset_proto,
            ),
        )

    def send(task_res: TaskRes) -> None:
        # Retrieve RecordSet and task_type
        recordset = serde.recordset_from_proto(task_res.task.recordset)
        task_type = task_res.task.task_type

        # RecordSet --> *Res --> *Res proto -> ClientMessage proto
        if task_type == TASK_TYPE_GET_PROPERTIES:
            getpropres = compat.recordset_to_getpropertiesres(recordset)
            msg_proto = ClientMessage(
                get_properties_res=serde.get_properties_res_to_proto(getpropres)
            )
        elif task_type == TASK_TYPE_GET_PARAMETERS:
            getparamres = compat.recordset_to_getparametersres(recordset, False)
            msg_proto = ClientMessage(
                get_parameters_res=serde.get_parameters_res_to_proto(getparamres)
            )
        elif task_type == TASK_TYPE_FIT:
            fitres = compat.recordset_to_fitres(recordset, False)
            msg_proto = ClientMessage(fit_res=serde.fit_res_to_proto(fitres))
        elif task_type == TASK_TYPE_EVALUATE:
            evalres = compat.recordset_to_evaluateres(recordset)
            msg_proto = ClientMessage(evaluate_res=serde.evaluate_res_to_proto(evalres))
        elif task_type == "reconnect":
            reason = cast(Reason.ValueType, recordset.get_configs("config")["reason"])
            msg_proto = ClientMessage(
                disconnect_res=ClientMessage.DisconnectRes(reason=reason)
            )
        else:
            raise ValueError(f"Invalid task type: {task_type}")

        # Send ClientMessage proto
        return queue.put(msg_proto, block=False)

    try:
        # Yield methods
        yield (receive, send, None, None)
    finally:
        # Make sure to have a final
        channel.close()
        log(DEBUG, "gRPC channel closed")
