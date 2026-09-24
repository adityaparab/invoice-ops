FROM docker.io/library/alpine:3.22.2@sha256:4b7ce07002c69e8f3d704a9c5d6fd3053be500b7f1c69fc0d80990c2ad8dd412

COPY minio /usr/local/bin/minio
RUN chmod 0555 /usr/local/bin/minio && mkdir -p /data

EXPOSE 9000 9001
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data", "--console-address", ":9001"]
