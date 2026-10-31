# Telegram Media Bot

Bot privado de Telegram para analisar links e baixar áudio MP3 ou vídeo usando yt-dlp e FFmpeg.

## Requisitos

- Windows 10/11
- Python 3.10 ou superior
- FFmpeg instalado e disponível no PATH
- Token criado com `@BotFather`
- IDs numéricos das pessoas autorizadas

## Configuração

1. Copie `.env.example` para `.env`.
2. Coloque o token do BotFather em `BOT_TOKEN`.
3. Coloque os IDs permitidos em `ALLOWED_USER_IDS`, separados por vírgula.
4. Instale o FFmpeg e confirme que `ffmpeg -version` funciona no terminal.
5. Execute `start.bat`.

Na primeira execução, o script cria o ambiente virtual e instala as dependências.

## Liberar amigos

Cada pessoa deve abrir o bot e enviar `/id`. O administrador adiciona o número recebido em `ALLOWED_USER_IDS` no `.env` e reinicia o bot.

Não compartilhe o token do bot. Se ele vazar, gere outro pelo BotFather.

## Observações

- O bot não contorna paywalls, DRM, autenticação ou controles de acesso.
- O suporte depende dos extratores disponíveis no yt-dlp e das mudanças dos próprios sites.
- O limite padrão de envio está configurado em 49 MB para evitar falhas na Bot API hospedada pelo Telegram.
- Para arquivos maiores, será necessário configurar uma Bot API Server local e ajustar os limites do projeto.
- Use apenas conteúdo que você tem autorização para baixar.
