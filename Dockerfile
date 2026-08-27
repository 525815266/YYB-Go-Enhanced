FROM golang:1.23-alpine AS build

WORKDIR /src
ENV GOPROXY=https://goproxy.cn,direct

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN go test ./...
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/yyb-go ./cmd/yyb-go

FROM alpine:3.21

RUN apk add --no-cache ca-certificates tzdata wget \
    && addgroup -S yyb \
    && adduser -S -G yyb -h /app yyb

WORKDIR /app
ENV TZ=Asia/Shanghai
COPY --from=build /out/yyb-go /app/yyb-go
COPY resource /tmp/resource-src

RUN mkdir -p /app/resource \
    && cp -R /tmp/resource-src/. /app/resource/ \
    && chown -R yyb:yyb /app/resource \
    && find /app/resource -type d -exec chmod 755 {} + \
    && find /app/resource -type f -exec chmod 644 {} + \
    && rm -rf /tmp/resource-src

USER yyb
EXPOSE 8000

ENTRYPOINT ["/app/yyb-go"]
CMD ["-host", "0.0.0.0", "-port", "8000", "-resource-root", "/app/resource"]
