FROM golang:1.23 AS build
COPY vendor/lib/ /src/vendor/lib/
RUN apt-get update && apt-get install -y make gcc
RUN go build ./...
