FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json .
COPY src/ src/
RUN npm run build && npm prune --omit=dev

ENV NODE_ENV=production

RUN addgroup -S nodegroup && adduser -S nodeuser -G nodegroup
RUN chown -R nodeuser:nodegroup /app
USER nodeuser

EXPOSE 4022

CMD ["node", "dist/index.js"]
