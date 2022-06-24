FROM ubuntu:22.04

RUN apt update && apt install pyside2-tools python3-pip wget fuse file -y
RUN pip3 install poetry
