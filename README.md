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

Na primeira execução, o script cria o ambiente virtual e instala as dependências fixadas em `requirements.txt`. Valide o projeto antes de atualizar essas versões.

## Liberar amigos

O token que constava no arquivo de exemplo anterior deve ser revogado no BotFather, mesmo que o arquivo tenha sido removido do diretório. Se o repositório foi compartilhado, remova também o segredo do histórico Git antes de compartilhá-lo novamente.

Cada pessoa deve abrir o bot e enviar `/id`. O administrador adiciona o número recebido em `ALLOWED_USER_IDS` no `.env` e reinicia o bot.

Não compartilhe o token do bot. Se ele vazar, gere outro pelo BotFather.

## Observações

- O bot valida os endereços públicos antes da análise e do download, mas sites podem redirecionar requisições. Para uso com usuários não confiáveis, restrinja também a saída de rede da máquina para impedir acesso a redes privadas; validação na aplicação não substitui firewall.
- Cada download usa uma pasta temporária exclusiva, removida após o envio. O limite é aplicado durante o download quando o tamanho é conhecido e conferido novamente antes do envio.
- O bot não contorna paywalls, DRM, autenticação ou controles de acesso.
- O suporte depende dos extratores disponíveis no yt-dlp e das mudanças dos próprios sites.
- O limite padrão de envio está configurado em 49 MB para evitar falhas na Bot API hospedada pelo Telegram.
- Para arquivos maiores, será necessário configurar uma Bot API Server local e ajustar os limites do projeto.
- Use apenas conteúdo que você tem autorização para baixar.

## Testes

Execute `.\.venv\Scripts\python.exe -m unittest discover -s tests` para validar as verificações de URL, limites e expiração dos pedidos.
