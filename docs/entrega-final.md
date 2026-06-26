# Entrega final

Este documento registra as melhorias feitas para a ultima entrega do projeto.
A ideia e manter um historico curto, objetivo e facil de revisar antes da
apresentacao ou envio final.

## Resumo

| Melhoria | Status | Resumo |
| --- | --- | --- |
| Crawler por workflow e release fixa | Feita | O GitHub Actions passou a restaurar, atualizar e republicar o pacote `data/` como asset da release `latest-data`. |
| Persistencia de coletas parciais | Feita | `tudo` passou a processar raws baixados antes de uma falha de fonte. |
| Progresso de execucao com Rich | Feita | Comandos longos passaram a exibir progresso por fonte, categoria e arquivo. |
| Dependencias centralizadas | Feita | O projeto passou a usar `pyproject.toml` como fonte unica de dependencias. |
| Script de pacote de dados | Feita | `scripts/cotacoes.py` passou a compactar e descompactar `ceasa-data-latest.tar.gz` com `tar` e `pigz`. |
| Otimizacoes de processamento | Feita | Raws ja persistidos podem ser ignorados, textos de PDFs passaram a ser cacheados e relatorios ganharam metricas de desempenho. |

## 1. Crawler por workflow e release fixa

### Objetivo

Executar coletas periodicas sem manter um processo Python permanente e manter
apenas o pacote de dados mais recente fora do Git comum.

### O que foi feito

- Removida a dependencia de armazenamento versionado para o pacote de dados.
- Mantida a estrategia de usar um arquivo compactado unico para representar a
  pasta `data/`.
- Criado o workflow `.github/workflows/scraper-release.yml` para restaurar o
  pacote atual, executar `docker compose run --rm tudo`, compactar `data/` e
  publicar `ceasa-data-latest.tar.gz` como asset da release fixa `latest-data`.
- Separadas actions locais para preparar configuracao, restaurar pacote,
  publicar asset e enviar relatorio por e-mail.
- Evitado versionar milhares de arquivos brutos diretamente no Git comum.

### Arquivos relacionados

- `.github/workflows/scraper-release.yml`
- `.github/actions/`
- `scripts/cotacoes.py`
- `.gitignore`
- `.dockerignore`

### Observacoes

- O clone do repositorio nao traz `data/`.
- O pacote mais recente deve ser baixado da release `latest-data`.
- O asset antigo da release e substituido pelo workflow, mantendo apenas a
  versao mais recente disponivel.
- Esse workflow e o crawler oficial do projeto por enquanto. Um daemon local
  fica reservado para necessidade futura de controle fino por fonte.

## 2. Persistencia de coletas parciais

### Objetivo

Aproveitar arquivos brutos que ja foram baixados quando uma fonte falha antes
de concluir todo o download.

### O que foi feito

- Criado um erro de download parcial que preserva a lista de raws salvos antes
  da falha.
- Ajustado o fluxo de todas as fontes para enviar esses raws para a fase de
  persistencia do `tudo`.
- Mantida a falha no resumo e no relatorio, sem tratar a fonte como concluida.
- Ajustada a execucao isolada com `--download-and-process` para processar os
  raws parciais antes de propagar o erro original.

### Arquivos relacionados

- `src/cotacoes_ceasa/workflows/collection.py`
- `src/cotacoes_ceasa/cli/commands/batch.py`
- `src/cotacoes_ceasa/cli/commands/source.py`
- `src/cotacoes_ceasa/cli/main.py`
- `docs/comandos.md`
- `docs/pendencias.md`

### Observacoes

- O processamento parcial ocorre somente para arquivos efetivamente salvos em
  disco antes da falha.
- Se a execucao inteira for interrompida antes da fase de persistencia, o
  comando `salvar` continua sendo a recuperacao manual.

## 3. Progresso de execucao com Rich

### Objetivo

Melhorar o acompanhamento de comandos longos, principalmente `tudo` e
`salvar`, exibindo fase atual, item em processamento, percentual, tempo
decorrido e estimativa quando o total e conhecido.

### O que foi feito

- Adicionada a dependencia `rich`.
- Criado um wrapper interno de progresso em `src/cotacoes_ceasa/cli/progress.py`.
- Integrado o progresso ao loop de fontes, categorias e arquivos raw.
- Mantida uma saida textual para execucoes sem terminal interativo, como logs
  de Docker.
- Registrados marcos de progresso no relatorio sem inflar os contadores de
  informacoes normais da CLI.

### Arquivos relacionados

- `pyproject.toml`
- `src/cotacoes_ceasa/cli/output.py`
- `src/cotacoes_ceasa/cli/progress.py`
- `src/cotacoes_ceasa/cli/commands/batch.py`
- `src/cotacoes_ceasa/workflows/collection.py`
- `src/cotacoes_ceasa/workflows/raw_processing.py`
- `docs/comandos.md`
- `docs/pendencias.md`

### Observacoes

- O Rich fica encapsulado no wrapper de progresso; os fluxos de negocio nao
  dependem diretamente da biblioteca.
- O wrapper reaproveita a mesma instancia de progresso quando ha tarefas
  aninhadas, preparando o caminho para paralelismo futuro.
- A estimativa aparece apenas quando o total e conhecido.

## 4. Dependencias centralizadas

### Objetivo

Evitar duplicacao entre arquivos de dependencia e manter o `pyproject.toml`
como ponto unico de manutencao.

### O que foi feito

- Removido o `requirements.txt`.
- Ajustado o Dockerfile para instalar o projeto com `pip install -e .`.
- Atualizada a documentacao de ambiente para apontar o `pyproject.toml`.
- Removida a pendencia menor sobre centralizacao de dependencias.

### Arquivos relacionados

- `Dockerfile`
- `pyproject.toml`
- `docs/ambiente.md`
- `docs/pendencias.md`

## 5. Script de pacote de dados

### Objetivo

Automatizar a compactacao e a restauracao da pasta `data/` usando o mesmo
formato de pacote publicado na release fixa.

### O que foi feito

- Ajustado `scripts/cotacoes.py` para operar com os comandos `compactar` e
  `descompactar`.
- Mantido o lock em `.cotacoes-data.lock` para impedir duas operacoes
  simultaneas no pacote.
- Automatizada a compactacao em `ceasa-data-latest.tar.gz.tmp`, validacao e
  substituicao segura do pacote final.
- Automatizada a restauracao de `ceasa-data-latest.tar.gz` para `data/`.
- Mantido o uso de `pigz` com `tar -I pigz` dentro do container.

### Arquivos relacionados

- `scripts/cotacoes.py`
- `Dockerfile`
- `.gitignore`
- `.dockerignore`
- `.github/workflows/scraper-release.yml`

### Observacoes

- No fluxo local, o host precisa apenas de Docker e Docker Compose.
- O workflow ainda usa Python no runner para o script de pacote.
- O pacote temporario so substitui o final depois que passa na validacao.
- O script nao executa mais o scraper; ele apenas compacta ou descompacta o
  pacote de dados.

## 6. Otimizacoes de processamento

### Objetivo

Reduzir trabalho repetido no processamento de raws sem alterar os resultados
dos parsers nem a estrutura dos dados persistidos.

### O que foi feito

- Adicionadas metricas de tempo por etapa no processamento de raws.
- Adicionado salto automatico de raws que ja existem em `coletas` com o mesmo
  `arquivo_raw` e `hash_raw`.
- Criado `--force-reprocess` para reprocessar tudo quando for necessario.
- Adicionado cache de texto extraido de PDFs em `data/cache/pdf-text/`.
- Adicionado cache em memoria para normalizacao de unidades repetidas.
- Reduzido o historico detalhado do relatorio, evitando registrar um `OK` para
  cada raw por padrao.
- Criado `--raw-detail-report` para reativar o detalhe arquivo por arquivo
  quando for necessario auditar uma execucao.

### Arquivos relacionados

- `src/cotacoes_ceasa/workflows/raw_processing.py`
- `src/cotacoes_ceasa/parsers/pdf.py`
- `src/cotacoes_ceasa/storage/sqlite.py`
- `src/cotacoes_ceasa/normalizers/unit.py`
- `src/cotacoes_ceasa/cli/parser.py`
- `src/cotacoes_ceasa/cli/output.py`
- `src/cotacoes_ceasa/cli/commands/source.py`
- `docs/comandos.md`
- `docs/pendencias.md`

### Observacoes

- O cache de PDF e derivado dos raws: se for apagado, ele pode ser recriado em
  uma nova execucao.
- O paralelismo de processamento ficou anotado como possibilidade futura, mas
  nao foi implementado nesta rodada.



