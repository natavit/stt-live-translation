FROM oven/bun:1
WORKDIR /app
COPY . .
RUN bun install

EXPOSE 3000/tcp

ENTRYPOINT ["bun", "run", "index.ts"]