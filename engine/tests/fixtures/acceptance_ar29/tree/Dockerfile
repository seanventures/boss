FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /out/northgate-gateway ./cmd/northgate-gateway
RUN go build -o /out/gwlint ./cmd/gwlint
RUN go build -o /out/echo-upstream ./mocks/echo-upstream

FROM alpine:3.20
COPY --from=build /out/northgate-gateway /usr/local/bin/northgate-gateway
COPY --from=build /out/gwlint /usr/local/bin/gwlin