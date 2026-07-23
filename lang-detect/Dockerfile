# No apk packages — alpine package mirrors often hang on restricted hosts.
FROM node:20-alpine AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:20-alpine

WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

COPY --from=build /app/dist ./dist

EXPOSE 3000
CMD ["npm", "start"]
