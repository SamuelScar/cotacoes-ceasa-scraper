# Cotacoes CEASA Scraper

Coleta cotacoes publicas de CEASAs brasileiras, preserva os arquivos brutos e
consolida os registros em um banco SQLite normalizado.

## Fluxo do projeto

1. Descobrir categorias e datas disponiveis em cada fonte.
2. Baixar HTMLs ou PDFs para `data/raw/<fonte>/`.
3. Processar os arquivos brutos com o parser da fonte.
4. Salvar cotacoes, proveniencia e versoes em `data/cotacoes.sqlite`.
5. Opcionalmente complementar campos vazios com o PROHORT.

Os coletores individuais continuam sendo a fonte principal. O PROHORT nao
sobrescreve valores ja preenchidos.

## Inicio rapido

Requisitos: Docker e Docker Compose.

```bash
cp .env.example .env
docker compose build
docker compose run --rm tudo
```

O servico `tudo` baixa os arquivos brutos de todas as fontes configuradas,
processa os raws ativos e salva o resultado no SQLite.

Comandos principais:

| Comando | Operacao |
| --- | --- |
| `docker compose run --rm baixar` | Baixa raws de todas as fontes |
| `docker compose run --rm salvar` | Reprocessa raws ativos e salva no SQLite |
| `docker compose run --rm tudo` | Baixa, processa e salva |
| `docker compose run --rm complementar-prohort` | Complementa o banco com PROHORT |
| `docker compose run --rm compactar-old` | Compacta HTMLs antigos |
| `docker compose run --rm app --help` | Exibe todas as opcoes da CLI |

## Janela de coleta

`COTACOES_TARGET_DATE` define a data limite. Quando fica vazio, cada fonte busca
a ultima cotacao disponivel.

`COTACOES_QUOTES_BACK` informa quantas datas de cotacao anteriores devem ser
coletadas. O valor nao representa dias corridos:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=99
```

Essa configuracao solicita a ultima data disponivel e mais 99 cotacoes
anteriores nas fontes que oferecem historico. CEASA-MG, CEASA-CE e CEASA-DF
coletam somente a publicacao atual.

## Arquivos gerados

- `data/raw/<fonte>/`: raws ativos usados no reprocessamento.
- `data/raw/<fonte>/old/`: versoes anteriores geradas no mesmo dia.
- `data/cotacoes.sqlite`: banco consolidado.

O processamento ignora `old/` e arquivos `.zip`. Quando o schema ou uma regra
de normalizacao mudar, exclua o SQLite e reconstrua o banco a partir dos raws
ativos.

## Documentacao

- [Ambiente e configuracao](docs/ambiente.md)
- [Comandos](docs/comandos.md)
- [Fontes e limitacoes](docs/fontes.md)
- [Modelo de dados](docs/modelo-dados.md)
- [Estrategias contra bloqueio](docs/estrategias-anti-bloqueio.md)
- [Decisoes tecnicas](docs/decisoes.md)
- [Pendencias](docs/pendencias.md)
