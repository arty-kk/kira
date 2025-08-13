module.exports = {
  apps: [
    {
      name: 'synchatica',
      cwd:  process.env.HOME + '/Synchatica',
      script: 'main.py',
      interpreter: './venv/bin/python3',
      watch: false,
      env: { PYTHONUNBUFFERED: 1 }
    }
  ]
};