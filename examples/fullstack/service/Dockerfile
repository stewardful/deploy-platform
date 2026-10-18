# Build off the 2GB deployment node; copy a statically linked Linux binary here.
FROM scratch
COPY fullstack-demo /fullstack-demo
USER 65532:65532
ENTRYPOINT ["/fullstack-demo"]
