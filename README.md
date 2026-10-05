# Cotacoes CEASA Scraper

Coleta cotacoes publicas de CEASAs brasileiras, preserva os arquivos brutos e consolida os registros em um banco SQLite normalizado.

## Inicio rapido

Requisitos: Docker e Docker Compose.

```bash
cp .env.example .env
docker compose build
docker compose run --rm tudo
```

Comandos principais:

| Comando | Operacao |
| --- | --- |
| `docker compose run --rm baixar` | Baixa raws de todas as fontes |
| `docker compose run --rm salvar` | Reprocessa raws ativos e salva no SQLite |
| `docker compose run --rm tudo` | Baixa, processa os raws da coleta e salva |
| `docker compose run --rm complementar-prohort` | Complementa o banco com PROHORT |
| `docker compose run --rm sincronizar-supabase` | Adiciona novos registros ao Supabase |
| `docker compose run --rm substituir-supabase` | Substitui completamente o Supabase |

## Documentacao

A documentacao detalhada fica na Wiki do repositorio:

- [Home da Wiki](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki)
- [Comandos de Operacao](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Comandos-de-Operacao)
- [Ambiente e Configuracao](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Ambiente-e-Configuracao)
- [Fluxo do Crawler](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Fluxo-do-Crawler)
- [Pacotes e Backups](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Pacotes-e-Backups)
- [Fontes e Limitacoes](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Fontes-e-Limitacoes)
- [Modelo de Dados](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Modelo-de-Dados)
- [Supabase](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Supabase)
- [Pendencias e Roadmap](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Pendencias-e-Roadmap)
- [Decisoes Tecnicas](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Decisoes-Tecnicas)

## Crawler atual

O crawler roda pelo GitHub Actions. Quando configurado, o OneDrive guarda o backup completo com raws, cache, logs operacionais e SQLite. A release fixa `latest-data` publica apenas o banco pronto para consumo em `cotacoes.sqlite.xz`.

Links uteis:

- [Actions](https://github.com/SamuelScar/cotacoes-ceasa-scraper/actions)
- [Release latest-data](https://github.com/SamuelScar/cotacoes-ceasa-scraper/releases/tag/latest-data)
- [Pacotes e Backups](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Pacotes-e-Backups)

## Logs das execucoes

Cada rodada do crawler possui um diretorio proprio em
`data/logs/execucao/<timestamp>_<run-id>_<run-attempt>/`. Execucoes locais usam
um identificador `local` unico e nao dependem das variaveis do GitHub Actions.

Os principais arquivos sao:

```text
resumo.md                  relatorio consolidado para leitura e email
resultado.json             resultado estruturado da rodada
execucao.json              estado, etapa atual, historico e arquivos gerados
console.log                saida do crawler e dos gates no workflow
etapas/scraper.md          relatorio detalhado da coleta
etapas/saude.json          avaliacao de saude
etapas/gate-checkpoint.json
etapas/gate-publicacao.json
etapas/supabase.md
etapas/publicacao.json     restauracao, pacotes, uploads e publicacao
```

O arquivo `_INCOMPLETA` indica uma rodada ainda em andamento ou interrompida
antes da consolidacao. A retencao nunca remove diretorios com esse marcador,
sem `execucao.json` valido ou sem a autorizacao `retention_safe`.

No workflow, `COTACOES_EXECUTION_RETENTION_DAYS` controla a idade maxima e
`COTACOES_EXECUTION_RETENTION_COUNT` a quantidade maxima de execucoes mantidas
no pacote completo. Ambos aceitam somente inteiros maiores ou iguais a 1; um
valor invalido interrompe a limpeza sem remover logs. Os padroes sao 30 dias e
120 execucoes. O artifact contém somente o diretorio da rodada atual e permanece
por 30 dias. Falhas no envio do email ou na preservacao do artifact tambem sao
incorporadas ao estado final e ao resumo do workflow.

Os arquivos existentes em `data/relatorios/` continuam legiveis durante a
migracao, mas o workflow nao os utiliza para escolher resumo, email ou artifact.

## Auditoria dos dados

A auditoria integral valida o SQLite, a proveniencia, os arquivos brutos, os
hashes registrados e o reprocessamento dos parsers sem alterar o banco. Os
logs de cada rodada ficam isolados em `data/logs/auditoria/`.

Execucao local completa:

```bash
docker compose run --rm \
  --user "$(id -u):$(id -g)" \
  --entrypoint python \
  app auditar_banco.py \
  --verify-raw \
  --full-integrity-check
```

O workflow `Auditar pacote de dados` tambem pode ser iniciado manualmente e
roda semanalmente aos domingos. Ele exige o pacote completo do OneDrive,
publica os logs como artifact por 90 dias e nao modifica nem republica o
pacote `latest`.

O status `consistente_com_alertas` nao reprova a rotina. A execucao falha
quando a auditoria e interrompida ou encontra ocorrencias classificadas como
erro, preservando mesmo assim os logs e relatorios produzidos.

## Licenca

Este projeto esta sob a licenca [GPL-3.0](LICENSE).
