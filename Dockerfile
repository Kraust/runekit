FROM ubuntu:22.04

RUN apt update && DEBIAN_FRONTEND=noninteractive apt install python3-pip wget file qtbase5-dev fuse -y
RUN pip3 install poetry
