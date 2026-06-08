# Arquitetura

Este projeto usa uma estrutura simples para permitir que cada fonte tenha seu proprio tratamento sem duplicar codigo comum.

## Ideia principal

Cada site de CEASA pode ter HTML, campos, filtros e formatos diferentes. Por isso, cada fonte deve ter um coletor proprio.

Mesmo assim, todos os coletores devem seguir o mesmo fluxo geral:

1. Baixar dados da fonte.
2. Extrair informacoes relevantes.
3. Normalizar valores.
4. Persistir os dados no formato do projeto.

## Estrutura

```text
src/
`-- cotacoes_ceasa/
    |-- collection.py
    |-- collectors/
    |-- contracts.py
    |-- http/
    |-- normalizers/
    |-- parsers/
    |-- raw_processing.py
    |-- source_registry.py
    |-- storage/
    |-- terminal.py
    `-- main.py
```

## Responsabilidades

### collection.py

Orquestra a coleta e o download por categoria, incluindo a resolucao das datas
que devem ser consultadas. Nao conhece detalhes de uma fonte especifica.

### collectors

Contem um arquivo por fonte.

Exemplos:

- `ceasa_pe.py`
- `ceasa_mg.py`

O coletor deve conhecer as particularidades da fonte, como nomes de colunas e estrutura da pagina.

URLs base devem vir de configuracao. Quando uma fonte expuser categorias por links na pagina base, o coletor deve descobrir essas categorias durante a execucao em vez de manter uma lista fixa no codigo.

Todos os coletores expoem as operacoes `discover_categories`,
`download_category` e `collect_category`.

### contracts.py

Define os contratos minimos usados pela orquestracao para operar coletores e
parsers sem depender de suas classes concretas.

### http

Contem codigo comum para requisicoes HTTP.

O objetivo e evitar que cada coletor implemente seu proprio `User-Agent`, timeout, intervalo entre requisicoes e tratamento basico de erro.

O intervalo entre requisicoes deve ser mantido para reduzir risco de bloqueio e evitar sobrecarregar fontes publicas.

### normalizers

Contem funcoes comuns para transformar dados brutos em valores padronizados.

Exemplos:

- Converter preco de texto para decimal.
- Converter data brasileira para `date`.
- Remover espacos duplicados.
- Criar slugs e chaves textuais comparaveis.
- Padronizar unidades.

### parsers

Contem a leitura do HTML bruto e a conversao para registros padronizados.

Cada fonte pode ter seu proprio parser quando a estrutura do HTML for especifica.
Leitura comum de paginas PDF fica em `parsers/pdf.py`; cada parser permanece
responsavel somente por interpretar o texto extraido.

### raw_processing.py

Le arquivos brutos ativos, extrai seus metadados pelo nome e os encaminha para
o parser da fonte. Tambem reconstroi a URL de origem e anexa caminho, hash e
data de download usados pela persistencia para registrar a coleta.

### source_registry.py

Mantem em um unico lugar a associacao entre cada fonte, seu coletor, seu parser
e suas opcoes especificas de construcao.

### storage

Contem a persistencia dos dados.

No inicio, existiam apenas arquivos para salvar HTML bruto. Agora esta camada tambem salva PDFs brutos quando a fonte entrega arquivos, mantendo a mesma regra de organizacao por fonte e categoria. A persistencia das cotacoes tratadas fica em SQLite.

O SQLite separa fontes, entrepostos, coletas, produtos, aliases e apresentacoes
de unidade. O storage recebe registros ja interpretados pelos parsers e nao
decide se um valor representa categoria, cidade ou procedencia.

### terminal.py

Padroniza cabecalhos, secoes, niveis de mensagem e resumos exibidos pela CLI.
As demais camadas informam o andamento por esse componente, sem definir seu
proprio formato de saida.

### main.py

Ponto de entrada da CLI.

Interpreta os argumentos, seleciona o fluxo solicitado e apresenta os
resultados. Nao concentra regras especificas de fontes nem processamento de
arquivos brutos.

## Regra para novas fontes

Para adicionar uma nova CEASA:

1. Adicionar a configuracao da fonte em `config/fontes.json`.
2. Criar um coletor em `collectors` e um parser em `parsers`.
3. Registrar coletor, parser e opcoes especificas em `source_registry.py`.
4. Reaproveitar `HttpClient` e os normalizadores existentes.
5. Criar funcoes comuns somente quando houver repeticao real.
6. Documentar a fonte em `docs/`.

## Docstrings

Use docstrings em:

- Classes publicas.
- Funcoes publicas.
- Funcoes cuja intencao nao seja obvia pelo nome.

Evite docstrings ou comentarios para codigo obvio.
