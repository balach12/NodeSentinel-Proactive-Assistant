#!/bin/bash
set -e

echo "Installing dependencies..."
sudo apt update && sudo apt install -y protobuf-compiler python3-grpcio-tools git

echo "Cloning LND repo..."
git clone https://github.com/lightningnetwork/lnd.git
cd lnd/lnrpc

echo "Generating Python files from rpc.proto..."
python3 -m grpc_tools.protoc --proto_path=. --python_out=. --grpc_python_out=. rpc.proto

echo "Copying generated files..."
cp lightning_pb2.py lightning_pb2_grpc.py ../../NodeSentinel-Proactive-Assistant/

echo "Done! Files available in NodeSentinel-Proactive-Assistant/"
