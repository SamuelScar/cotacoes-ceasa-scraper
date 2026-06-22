# Entrega final

Este documento registra as melhorias feitas para a ultima entrega do projeto.
A ideia e manter um historico curto, objetivo e facil de revisar antes da
apresentacao ou envio final.

## Resumo

| Melhoria | Status | Resumo |
| --- | --- | --- |
| Git LFS para os dados compactados | Feita | `data.tar.gz` passou a ser tratado pelo Git LFS para evitar que o Git comum carregue o arquivo grande diretamente. |
| Persistencia de coletas parciais | Feita | `tudo` passou a processar raws baixados antes de uma falha de fonte. |
| Progresso de execucao com Rich | Feita | Comandos longos passaram a exibir progresso por fonte, categoria e arquivo. |
| Dependencias centralizadas | Feita | O projeto passou a usar `pyproject.toml` como fonte unica de dependencias. |
| Wrapper operacional do pacote de dados | Feita | `scripts/cotacoes.py` passou a controlar descompactacao, execucao e recompactacao segura de `data.tar.gz`. |
| Otimizacoes de processamento | Feita | Raws ja persistidos podem ser ignorados, textos de PDFs passaram a ser cacheados e relatorios ganharam metricas de desempenho. |

## 1. Git LFS para os dados compactados

### Objetivo

Reduzir o impacto do arquivo grande de dados no repositorio Git comum,
mantendo a possibilidade de versionar o pacote de dados usado pelo projeto.

### O que foi feito

- Configurado `data.tar.gz` no `.gitattributes` para ser controlado pelo Git LFS.
- Mantida a estrategia de usar um arquivo compactado unico para representar a
  pasta `data/`.
- Evitado versionar milhares de arquivos brutos diretamente no Git comum.

### Arquivos relacionados

- `.gitattributes`
- `data.tar.gz`
- `docs/pendencias.md`

### Observacoes

- O Git LFS melhora o clone e o controle do repositorio, mas nao elimina o
  custo de armazenamento remoto.
- Cada nova versao enviada de `data.tar.gz` ainda consome espaco no provedor
  Git LFS.
- Para novas maquinas, e necessario ter Git LFS instalado antes de baixar os
  dados corretamente.

### Complemento posterior

O fluxo operacional seguro para atualizar `data.tar.gz` foi implementado depois
no wrapper descrito no item 5.

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

## 5. Wrapper operacional do pacote de dados

### Objetivo

Automatizar o ciclo de uso do pacote de dados, mantendo `data.tar.gz` como
artefato versionado e usando `data/` apenas durante a execucao.

### O que foi feito

- Criado `scripts/cotacoes.py` como wrapper multiplataforma para Ubuntu e
  Windows.
- Adicionado lock em `.cotacoes-data.lock` para impedir duas execucoes
  simultaneas alterando o pacote.
- Automatizada a descompactacao de `data.tar.gz` para `data/`.
- Automatizada a execucao do servico Docker solicitado.
- Automatizada a compactacao em `data.tar.gz.tmp`, validacao e substituicao
  segura do pacote final.
- Ajustado o Dockerfile para instalar `pigz`, usado com `tar -I pigz` para
  compactar e descompactar com multiplas threads dentro do container.
- Atualizado `.gitignore` para ignorar lock e pacote temporario.
- Removida a pendencia de fluxo operacional do pacote de dados.

### Arquivos relacionados

- `scripts/cotacoes.py`
- `Dockerfile`
- `.gitignore`
- `docs/comandos.md`
- `docs/ambiente.md`
- `docs/pendencias.md`

### Observacoes

- O host precisa apenas de Python, Docker e Docker Compose.
- O `data.tar.gz` antigo so e substituido depois que o pacote temporario passa
  na validacao.
- Se a compactacao falhar, o pacote anterior permanece preservado e `data/`
  fica em disco para recuperacao.

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
