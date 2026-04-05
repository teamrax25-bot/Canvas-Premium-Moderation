Canvas Premium Moderation Bot - Koyeb Ready Package

Files:
- bot.py
- requirements.txt
- config.json
- .env.example
- Dockerfile
- .dockerignore

Before deploy:
1. Rename .env.example to .env if running locally
2. Put your real Discord token in environment variables on Koyeb
3. Replace owner_id in config.json with your Discord user ID

Koyeb deploy:
- Push these files to GitHub
- Create App on Koyeb
- Choose your GitHub repo
- Service type: Worker
- Build with Dockerfile
- Add env var: DISCORD_TOKEN
