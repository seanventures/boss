FROM node:22-slim AS web-build
WORKDIR /app/web
COPY web/package.json ./
RUN npm install --no-audit --no-fund --loglevel=error
COPY web/ ./
RUN npm run build

FROM node:22-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund --loglevel=error
COPY server/ ./server/
COPY --from=web-build /app/web/dist ./web/dist

ENV NODE_ENV=production
EXPOSE 3000
CMD ["node", "server/index.mjs"]
