# Cotacoes CEASA Scraper

Projeto para coletar cotacoes publicas de CEASAs brasileiras, padronizar os dados e consolidar as informacoes em uma base unica para consulta e analise historica.

## Objetivo

Construir um scraper simples e extensivel para extrair cotacoes de produtos hortifrutigranjeiros em diferentes centrais de abastecimento do Brasil.

O foco inicial foi trabalhar com fontes publicas em HTML ou APIs abertas. Com a base funcionando, o projeto tambem passou a tratar PDFs quando a fonte oferece uma estrutura estavel.

## O que sera feito

- Identificar e acessar fontes publicas de cotacoes de CEASAs e centrais de abastecimento.
- Extrair dados de produtos hortifrutigranjeiros.
- Normalizar nomes de produtos, unidades, estados e valores.
- Armazenar os registros em SQLite.
- Manter a coleta extensivel para novas fontes.

## Dados esperados

Cada cotacao deve seguir um formato padrao, independentemente da fonte:

- CEASA de origem.
- Estado e cidade.
- Produto.
- Unidade.
- Data da cotacao.
- Preco minimo.
- Preco comum.
- Preco maximo.
- Procedencia, classificacao e situacao de mercado, quando existirem.
- URL de origem.
- Data da coleta.

## Fontes previstas

- CEASA-PE: https://www.ceasape.org.br/cotacao
- CEASA-MG: https://minas1.ceasa.mg.gov.br/ceasainternet/cst_precosmaiscomumMG/cst_precosmaiscomumMG.php
- CEASA-PR: https://www.ceasa.pr.gov.br/Pagina/Cotacao-Diaria-de-Precos
- CEAGESP-SP: https://ceagesp.gov.br/cotacoes/
- CEASA-RJ: https://www.rj.gov.br/ceasa/Cota%C3%A7%C3%A3o
- CEASA-DF: https://www.portal.ceasadf.com.br/informacao-mercado
- CEASA Campinas: https://www.ceasacampinas.com.br/cotacoes-anteriores
- CEASA-GO: https://goias.gov.br/ceasa/cotacoes-diarias/
- CEASA-BA: https://www.ba.gov.br/sde/boletim-informativo-ceasa
- CEASA-CE: https://files.ceasa-ce.com.br/unsima/boletim_diario/boletim.php
- CEASA-ES: https://ceasa.es.gov.br/boletim

## Escopo e restricoes

- Priorizar fontes com dados HTML ou API publica.
- Tratar tabelas e paginas estaveis primeiro.
- Fontes em PDF podem ser tratadas quando tiverem links publicos e estrutura estavel.
- Coletar o maior numero possivel de fontes viaveis, respeitando o formato e a disponibilidade de cada CEASA.
- Usar SQLite como arquivo final de armazenamento.

## Modelo de dados atual

A versao atual usa SQLite com tabelas normalizadas:

- `estados`
- `ceasas`
- `categorias`
- `produtos`
- `unidades`
- `cotacoes`

Detalhes em [Modelo de dados](docs/modelo-dados.md).

## Exemplo de registro

- CEASA: CEAGESP-SP.
- Produto: Banana Prata.
- Unidade: kg.
- Data da cotacao: 27/05/2026.
- Preco minimo: 4,50.
- Preco comum: 5,20.
- Preco maximo: 6,00.
- Fonte: pagina da CEAGESP.
- Data da coleta: 27/05/2026.

## Documentacao

- [Ambiente de desenvolvimento](docs/ambiente.md)
- [Comandos](docs/comandos.md)
- [Fontes de dados](docs/fontes.md)
- [Avaliacao de fontes](docs/avaliacao-fontes.md)
- [Progresso das fontes](docs/progresso-fontes.md)
- [Modelo de dados](docs/modelo-dados.md)
- [Plano de implementacao](docs/plano-implementacao.md)
- [Decisoes tecnicas](docs/decisoes.md)
- [Arquitetura](docs/arquitetura.md)

## Estrategia inicial

1. Mapear as fontes e classificar a viabilidade de cada uma.
2. Criar a estrutura minima do projeto.
3. Implementar a coleta inicial da CEASA-PE.
4. Salvar os dados em SQLite.
5. Expandir para novas fontes mantendo o mesmo formato de saida.

## Principios do projeto

- Manter o codigo simples e legivel.
- Separar coleta, extracao, normalizacao e persistencia.
- Documentar decisoes tecnicas importantes.
- Priorizar fontes estaveis antes de lidar com casos complexos.
- Evitar refatoracoes grandes sem necessidade.

## Ambiente com Docker

O projeto roda via Docker para manter os mesmos comandos no Windows e no Linux.

Copiar o arquivo de exemplo de variaveis locais:

```bash
cp .env.example .env
```

Buildar a imagem:

```bash
docker compose build
```

Baixar os arquivos brutos de todas as fontes:

```bash
docker compose run --rm baixar
```

Processar os arquivos brutos baixados e salvar no SQLite:

```bash
docker compose run --rm salvar
```

Baixar, processar e salvar no SQLite em um unico comando:

```bash
docker compose run --rm tudo
```

Compactar HTMLs antigos da pasta `old`:

```bash
docker compose run --rm compactar-old
```

Esse comando gera um novo `.zip` em `data/raw/<fonte>/old/` e remove os `.html` que entraram no arquivo compactado.

Complementar cotacoes ja salvas com o PROHORT:

```bash
docker compose run --rm complementar-prohort
```

Esse comando nao substitui o fluxo principal. Ele deve ser executado depois dos scrapers individuais. Quando encontra uma correspondencia confiavel no PROHORT, preenche campos vazios; quando o PROHORT tem produto do mesmo dia e da mesma CEASA que a fonte principal nao trouxe, insere uma cotacao complementar.

Para evitar downloads repetidos quando o raw ja existe na pasta principal:

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```

Executar a ajuda da CLI:

```bash
docker compose run --rm app --help
```

O diretorio do projeto e montado em `/app`, entao arquivos gerados em `data/` ficam salvos na maquina.

As dependencias externas ficam registradas no `requirements.txt` e no `pyproject.toml`.

## Resultado esperado

Uma base SQLite integrada de cotacoes que permita comparar precos entre estados e consultar historico de produtos hortifrutigranjeiros comercializados no atacado.
