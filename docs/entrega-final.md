# Entrega final

Este documento registra as melhorias feitas para a ultima entrega do projeto.
A ideia e manter um historico curto, objetivo e facil de revisar antes da
apresentacao ou envio final.

## Resumo

| Melhoria | Status | Resumo |
| --- | --- | --- |
| Crawler por workflow e release fixa | Feita | O GitHub Actions passou a restaurar, atualizar e republicar o pacote `data/` como asset da release `latest-data`. |
| Persistencia de coletas parciais | Feita | `tudo` passou a processar raws baixados antes de uma falha de fonte. |
| Pipeline de download e persistencia | Feita | `tudo` com mais de um worker passou a processar cada fonte assim que seu download termina. |
| Progresso de execucao com Rich | Feita | Comandos longos passaram a exibir progresso por fonte, categoria e arquivo. |
| Dependencias centralizadas | Feita | O projeto passou a usar `pyproject.toml` como fonte unica de dependencias. |
| Script de pacote de dados | Feita | `scripts/cotacoes.py` passou a compactar e descompactar `ceasa-data-latest.tar.gz` com `tar` e `pigz`. |
| Backup externo do pacote | Feita | O workflow salva o pacote completo no OneDrive e publica no GitHub apenas o pacote enxuto. |
| Otimizacoes de processamento | Feita | Raws ja persistidos podem ser ignorados, textos de PDFs passaram a ser cacheados e relatorios ganharam metricas de desempenho. |
| Robustez da CEASA-PR | Feita | A persistencia passou a rejeitar cotacoes invalidas sem derrubar o lote, preencher data ausente pelo raw quando seguro e recuperar PDFs malformados com fallback global. |
| Tratamento de raws sem cotacao | Feita | A CEASA-PE passou a diferenciar paginas sem cotacao de falhas reais e o relatorio passou a separar raws com e sem cotacao. |
| Historico limitado por fonte | Feita | CEAGESP-SP e CEASA-BA passaram a ter limite proprio de historico e o relatorio informa quando o `quotes_back` efetivo foi reduzido. |

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
- Se a execucao inteira for interrompida antes de algum raw entrar na
  persistencia, o comando `salvar` continua sendo a recuperacao manual.

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

- Ajustado `scripts/cotacoes.py` para operar dentro do container com os comandos
  `compactar` e `descompactar`.
- Mantido o lock em `.cotacoes-data.lock` para impedir duas operacoes
  simultaneas no pacote.
- Automatizada a compactacao em `ceasa-data-latest.tar.gz.tmp`, validacao e
  substituicao segura do pacote final.
- Automatizada a restauracao de `ceasa-data-latest.tar.gz` para `data/`.
- Mantido o uso de `pigz` com `tar -I pigz` dentro do container.
- Adicionado `--incluir-sqlite` para permitir pacote completo no OneDrive,
  mantendo o pacote padrao sem SQLite para reduzir tamanho na release.

### Arquivos relacionados

- `scripts/cotacoes.py`
- `Dockerfile`
- `.gitignore`
- `.dockerignore`
- `.github/workflows/scraper-release.yml`

### Observacoes

- O host precisa apenas de Docker e Docker Compose; Python, `tar` e `pigz` sao
  usados a partir do container `app`.
- O script recusa execucao direta no host para evitar depender de ferramentas
  instaladas fora da imagem.
- O pacote temporario so substitui o final depois que passa na validacao.
- O script nao executa mais o scraper; ele apenas compacta ou descompacta o
  pacote de dados.

## 6. Backup externo do pacote

### Objetivo

Manter uma copia automatica do pacote fora do GitHub antes de substituir o asset
da release fixa.

### O que foi feito

- Criada a action `.github/actions/backup-release-data` para enviar o pacote ao
  OneDrive com `rclone` antes da publicacao na release.
- O backup externo mantem um pacote completo com SQLite em `latest/` e uma copia
  historica em `history/`.
- A restauracao passou a tentar primeiro o pacote completo do OneDrive e usar o
  pacote enxuto da release como fallback.
- O workflow passou a continuar para compactacao e salvamento mesmo quando o
  scraper termina com erro, validando no fim se algum pacote saiu do runner.
- O ultimo relatorio em `data/relatorios` passou a receber uma secao com os
  resultados das etapas de restauracao, backup, publicacao e validacao.
- A publicacao na release valida o tamanho do pacote antes de remover qualquer
  asset antigo, evitando perda quando o GitHub recusar arquivos grandes.
- O pacote da release continua sem SQLite para reduzir tamanho.

### Arquivos relacionados

- `.github/actions/backup-release-data/action.yml`
- `.github/actions/restore-release-data/action.yml`
- `.github/actions/publish-release-data/action.yml`
- `.github/workflows/scraper-release.yml`
- `docs/ambiente.md`
- `docs/comandos.md`

## 7. Otimizacoes de processamento

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

## 8. Pipeline de download e persistencia

### Objetivo

Aproveitar o tempo ocioso entre requisicoes HTTP processando fontes que ja
terminaram o download, sem abrir escrita concorrente no SQLite.

### O que foi feito

- Criado um fluxo em pipeline para `tudo` quando `COTACOES_WORKERS` ou
  `--workers` for maior que `1`.
- Mantidos varios produtores de download com `ThreadPoolExecutor`.
- Usada uma fila em memoria baseada nos resultados concluidos por fonte,
  carregando apenas metadados, logs bufferizados e caminhos dos raws.
- Mantido um unico consumidor de persistencia, executando processamento e
  escrita no SQLite de forma sequencial.
- Preservado o relatorio completo com eventos de download, processamento,
  persistencia, falhas parciais e resumo consolidado do pipeline.
- Adicionadas metricas de desempenho do pipeline no relatorio, incluindo
  tempos de download e persistencia, espera na fila, backlog maximo, estimativa
  sem sobreposicao, ganho estimado por sobreposicao e detalhamento por fonte.

### Arquivos relacionados

- `src/cotacoes_ceasa/cli/commands/batch.py`
- `docs/comandos.md`
- `docs/pendencias.md`

### Observacoes

- Os arquivos HTML e PDF continuam em disco; a fila nao carrega o conteudo dos
  raws em memoria.
- `workers=1` continua usando o fluxo sequencial anterior.
- O processamento paralelo de raws/PDFs continua fora desta etapa.
- As metricas permitem comparar rodadas com diferentes valores de
  `COTACOES_WORKERS` sem depender apenas da duracao total da action.

## 9. Ajustes pos-analise dos relatorios de 2026-07-07

### Objetivo

Corrigir os pontos de maior perda ou ruido identificados nos relatorios mais
recentes do crawler, principalmente CEASA-PR, CEASA-PE e CEAGESP-SP.

### O que foi feito

- Tornada a persistencia mais tolerante a registros invalidos no fluxo de
  `--process-raw` e `--save`, separando cotacoes validas de rejeitadas antes da
  escrita no SQLite.
- Adicionado resumo de `Cotacoes rejeitadas` para deixar claro quando algum
  registro foi descartado por falta de data, falta de preco ou preco negativo.
- Ajustado o parser da CEASA-PR para ignorar cabecalhos adicionais que estavam
  virando pseudo-produtos.
- Adicionado preenchimento de data ausente da CEASA-PR a partir da data do raw,
  quando o parser nao encontra data no texto extraido.
- Ajustado o parser da CEASA-PE para tratar paginas 404 internas e paginas sem
  registro como ausencia de cotacao, nao como erro de parser.
- Adicionadas metricas de `Raws com cotacao` e `Raws sem cotacao` ao
  processamento de raws.
- Adicionado fallback global de extracao de PDF com `pdftotext`, acionado apenas
  quando o `pypdf` falha em PDFs malformados.
- Adicionada a dependencia `poppler-utils` na imagem Docker para disponibilizar
  `pdftotext` dentro do container.
- Adicionada a metrica `Fallback PDF pdftotext` ao relatorio de processamento.
- Marcada a CEAGESP-SP como fonte de historico limitado em `config/fontes.json`, com `max_quotes_back=10`.
- Marcada a CEASA-BA como fonte de historico limitado em `config/fontes.json`, com `max_quotes_back=1`, porque a pagina passou a expor apenas uma janela recente e pode bloquear algumas origens.
- Adicionado calculo de `quotes_back` efetivo por fonte, preservando o valor
  solicitado no relatorio quando houver reducao.
- Ajustada a resolucao historica para aceitar as datas encontradas em fontes
  limitadas, sem transformar historico curto em falha quando ha ao menos uma
  data valida.

### Validacoes observadas

- CEASA-PR deixou de falhar por `Cotacao sem data` no teste isolado e persistiu
  `530163` cotacoes, sem rejeicoes.
- CEASA-PE deixou de registrar avisos de tabela ausente no teste isolado e
  processou `4088` raws, separando `3201` com cotacao e `887` sem cotacao.
- A investigacao dos PDFs problematicos da CEASA-PR mostrou que os `26` avisos
  correspondiam a apenas `5` PDFs unicos repetidos em coletas diferentes.
- Esses PDFs foram confirmados como malformados pelo `qpdf`, mas com texto
  recuperavel por `pdftotext`.
- A CEAGESP-SP ainda precisa ser validada em uma execucao Docker, mas a regra
  agora limita a busca efetiva a `10` cotacoes anteriores e registra a reducao
  no relatorio.
- A CEASA-BA foi tratada como fonte limitada e bloqueavel; quando o download
  oficial falhar ou a janela exposta for curta, o complemento PROHORT pode
  reduzir perda de cotacoes correspondentes.

### Arquivos relacionados

- `Dockerfile`
- `config/fontes.json`
- `src/cotacoes_ceasa/config.py`
- `src/cotacoes_ceasa/parsers/pdf.py`
- `src/cotacoes_ceasa/parsers/ceasa_pe.py`
- `src/cotacoes_ceasa/parsers/ceasa_pr.py`
- `src/cotacoes_ceasa/workflows/raw_processing.py`
- `src/cotacoes_ceasa/workflows/collection.py`
- `src/cotacoes_ceasa/cli/commands/source.py`
- `.github/workflows/scraper-release.yml`
- `.github/actions/prepare-scraper/action.yml`

### Observacoes

- O fallback de PDF e global, mas nao substitui o caminho principal: PDFs normais
  continuam sendo extraidos com `pypdf`.
- Para validar o fallback no ambiente oficial, a imagem Docker precisa ser
  reconstruida para instalar `poppler-utils`.
- Ainda falta rodar uma execucao completa de todas as fontes apos essas
  alteracoes para medir o impacto consolidado no relatorio final.
- Para CEAGESP-SP, `quotes_back=100` passa a ser limitado para `10` internamente;
  para CEASA-BA, passa a ser limitado para `1`; outras fontes continuam usando o
  valor solicitado, salvo configuracao propria.
- O workflow agora respeita `COTACOES_COMPLEMENT_PROHORT` definido no Environment
  `Crawler` e repassa o valor para o `.env` gerado no runner.
