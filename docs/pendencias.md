# Pendencias

## Versionamento dos dados

Definir um fluxo seguro para versionar a pasta `data/` compactada:

- descompactar antes de comandos do container;
- compactar novamente ao encerrar, inclusive em falhas ou interrupcoes;
- ajustar o `.gitignore`;
- substituir o arquivo compactado de forma atomica para evitar perda de dados.
