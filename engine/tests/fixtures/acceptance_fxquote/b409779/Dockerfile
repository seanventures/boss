FROM golang:1.23-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /fxquote ./cmd/fxquote

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=build /fxquote /app/fxquote
COPY rates.yaml /app/rates.yaml
ENV FXQUOTE_CONFIG=/app/rates.yaml
EXPOSE 8080
CMD ["/app/fxquote"]
