FROM node:18-alpine

# Set working directory
WORKDIR /app

# Copy package files
COPY package.json package-lock.json* ./

# Install dependencies
RUN npm install

# Copy source code
COPY . .

# Build the project (if needed for production, otherwise just expose dev)
# Here we'll start the development server
EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host"]
