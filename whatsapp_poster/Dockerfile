# The WhatsApp attendance poster: a browser that stays logged in to WhatsApp
# Web and posts each branch's attendance for the day, as text, at the times in
# RUN_TIMES.
#
# The official Playwright image, at the same version as the playwright package
# in package.json -- the two have to match or the browser will not start.
FROM mcr.microsoft.com/playwright:v1.64.0-noble

ENV NODE_ENV=production \
    DATA_DIR=/data \
    TZ=Asia/Kolkata \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY src ./src

# The WhatsApp session, and a copy of each message sent. MOUNT A VOLUME HERE in
# Dokploy, or the phone has to be linked again after every deploy.
VOLUME ["/data"]

EXPOSE 3000

CMD ["node", "src/index.js"]
