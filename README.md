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

O crawler roda pelo GitHub Actions. Quando configurado, o OneDrive guarda o backup completo com raws, cache, relatorios e SQLite. A release fixa `latest-data` publica apenas o banco pronto para consumo em `cotacoes.sqlite.xz`.

Links uteis:

- [Actions](https://github.com/SamuelScar/cotacoes-ceasa-scraper/actions)
- [Release latest-data](https://github.com/SamuelScar/cotacoes-ceasa-scraper/releases/tag/latest-data)
- [Pacotes e Backups](https://github.com/SamuelScar/cotacoes-ceasa-scraper/wiki/Pacotes-e-Backups)

## Licenca

Este projeto esta sob a licenca [GPL-3.0](LICENSE).
