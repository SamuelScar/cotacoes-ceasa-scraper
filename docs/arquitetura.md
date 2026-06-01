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
    |-- collectors/
    |-- http/
    |-- normalizers/
    |-- storage/
    `-- main.py
```

## Responsabilidades

### collectors

Contem um arquivo por fonte.

Exemplos:

- `ceasa_pe.py`
- `ceasa_mg.py`

O coletor deve conhecer as particularidades da fonte, como nomes de colunas e estrutura da pagina.

URLs base devem vir de configuracao. Quando uma fonte expuser categorias por links na pagina base, o coletor deve descobrir essas categorias durante a execucao em vez de manter uma lista fixa no codigo.

### http

Contem codigo comum para requisicoes HTTP.

O objetivo e evitar que cada coletor implemente seu proprio `User-Agent`, timeout, intervalo entre requisicoes e tratamento basico de erro.

O intervalo entre requisicoes deve ser mantido para reduzir risco de bloqueio e evitar sobrecarregar fontes publicas.

### normalizers

Deve conter funcoes comuns para transformar dados brutos em valores padronizados.

Exemplos futuros:

- Converter preco de texto para decimal.
- Converter data brasileira para `date`.
- Remover espacos duplicados.
- Padronizar unidades.

### parsers

Contem a leitura do HTML bruto e a conversao para registros padronizados.

Cada fonte pode ter seu proprio parser quando a estrutura do HTML for especifica.

### storage

Contem a persistencia dos dados.

No inicio, existem apenas arquivos para salvar HTML bruto. Depois esta camada tambem deve salvar as cotacoes tratadas em SQLite.

### main.py

Ponto de entrada da CLI.

Deve orquestrar a coleta sem concentrar regra especifica de uma fonte.

## Regra para novas fontes

Para adicionar uma nova CEASA:

1. Criar um novo arquivo em `collectors`.
2. Reaproveitar `HttpClient` para baixar dados.
3. Reaproveitar normalizadores existentes quando fizer sentido.
4. Criar funcoes comuns somente quando houver repeticao real.
5. Documentar a fonte em `docs/`.

## Docstrings

Use docstrings em:

- Classes publicas.
- Funcoes publicas.
- Funcoes cuja intencao nao seja obvia pelo nome.

Evite docstrings ou comentarios para codigo obvio.
