# CEASA-PE

Documentacao da primeira fonte de dados do projeto.

## Fonte

URL principal:

- https://www.ceasape.org.br/cotacao

Configuracao:

- `config/fontes.json`

Categorias:

As categorias sao descobertas automaticamente a partir dos links da pagina base.

Quando `COTACOES_CATEGORY=todas`, o coletor acessa a URL base, identifica os links abaixo de `/cotacao/` e baixa cada categoria encontrada.

## Primeira etapa

A primeira etapa baixa o HTML bruto das paginas de cotacao e salva em `data/raw/ceasa-pe/`.

Para cada fonte, categoria, data de cotacao consultada e dia de execucao, a pasta principal mantem somente o HTML bruto mais recente. Quando uma nova coleta salva outro arquivo do mesmo grupo no mesmo dia, o arquivo anterior e movido para `data/raw/ceasa-pe/old/`.

Tambem existe um parser inicial para transformar a tabela HTML em registros normalizados em memoria.

Status: implementado para uma categoria especifica ou todas as categorias descobertas na pagina base.

## Como executar

Baixar todas as categorias descobertas na pagina base:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main
```

Listar categorias descobertas sem baixar todas as tabelas:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --list-categories
```

Baixar e extrair todas as categorias descobertas:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --parse
```

Baixar, extrair e salvar no SQLite:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --save
```

Baixar uma categoria especifica:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --source ceasa-pe --category aves-e-ovos
```

## Validacao manual feita

Em 2026-06-01, a categoria `cereais-e-diversos` foi baixada com sucesso.

Arquivo gerado:

- `data/raw/ceasa-pe/cereais-e-diversos_20260601_160726.html`

O HTML baixado contem uma tabela com os cabecalhos esperados:

- `Produto`
- `Und.`
- `Proced.`
- `Tipo`
- `Pr.Min.`
- `Pr.M.Com.`
- `Pr.Max.`
- `Sit.Merc.`

Em 2026-06-01, a categoria `aves-e-ovos` foi baixada e extraida com sucesso.

Arquivo gerado:

- `data/raw/ceasa-pe/aves-e-ovos_20260601_162054.html`

Resultado do parser:

- 9 cotacoes extraidas para `aves-e-ovos`.
- 326 cotacoes extraidas para todas as categorias descobertas.

Resultado da gravacao em SQLite:

- 9 registros novos salvos em `data/cotacoes.sqlite`.
- Consulta de validacao retornou 9 registros na tabela `cotacoes`.
- 317 registros novos salvos ao rodar todas as categorias, pois 9 registros ja existiam no banco.
- Apos migrar para o schema relacional, a categoria `aves-e-ovos` salvou 9 cotacoes no novo modelo.
- Em 29/05/2026, foram salvas 216 cotacoes no schema relacional.
- Em 29/05/2026, `flores` e `organicos` nao retornaram tabela de cotacoes; as demais categorias foram processadas normalmente.

Contagens observadas no schema relacional apos salvar `aves-e-ovos`:

| Tabela | Registros |
| --- | ---: |
| estados | 1 |
| ceasas | 1 |
| categorias | 1 |
| produtos | 3 |
| unidades | 2 |
| cotacoes | 9 |

Contagens observadas para 29/05/2026:

| Categoria | Registros |
| --- | ---: |
| aves-e-ovos | 9 |
| carnes-e-laticinios | 15 |
| cereais-e-diversos | 16 |
| frutas | 83 |
| hortalicas | 81 |
| pescados | 12 |

Comando usado no teste:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --parse
```

## Proximo passo

Depois de validar a CEASA-PE completa, o proximo passo e testar a coleta por datas de cotacao anteriores e melhorar a tolerancia a falhas por categoria.
