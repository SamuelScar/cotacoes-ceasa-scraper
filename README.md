# Cotacoes CEASA Scraper

Projeto para coletar cotacoes publicas de CEASAs brasileiras, padronizar os dados e consolidar as informacoes em uma base unica para consulta e analise historica.

## Objetivo

Construir um scraper simples e extensivel para extrair cotacoes de produtos hortifrutigranjeiros em diferentes centrais de abastecimento do Brasil.

O foco inicial e trabalhar com fontes publicas em HTML ou APIs abertas. Fontes em PDF, paginas instaveis ou com bloqueios devem ser avaliadas depois que a base do projeto estiver funcionando.

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
- CEASA-RJ: https://www.rj.gov.br/ceasa/Cotação
- CEASA-DF: https://www.portal.ceasadf.com.br/precos
- CEASA Campinas: https://www.ceasacampinas.com.br/cotacoes-anteriores
- CEASA-GO: https://goias.gov.br/ceasa/cotacoes-diarias/
- CEASA-BA: https://www.ba.gov.br/sde/boletim-informativo-ceasa
- CEASA-CE: https://files.ceasa-ce.com.br/unsima/boletim_diario/boletim.php
- CEASA-ES: https://ceasa.es.gov.br/boletim

## Escopo e restricoes

- Priorizar fontes com dados HTML ou API publica.
- Tratar tabelas e paginas estaveis primeiro.
- Fontes em PDF ou com muitos bloqueios podem ficar fora do escopo inicial.
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
- [Modelo de dados](docs/modelo-dados.md)
- [Plano de implementacao](docs/plano-implementacao.md)
- [Decisoes tecnicas](docs/decisoes.md)
- [Arquitetura](docs/arquitetura.md)
- [CEASA-PE](docs/ceasa-pe.md)

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

## Ambiente local

Criar o ambiente virtual:

```bash
python3 -m venv .venv
```

Ativar o ambiente:

```bash
source .venv/bin/activate
```

Copiar o arquivo de exemplo de variaveis locais:

```bash
cp .env.example .env
```

Instalar dependencias:

```bash
pip install -r requirements.txt
```

Executar a coleta inicial:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main
```

Baixar e extrair a categoria configurada no `.env`:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --parse
```

Baixar, extrair e salvar no SQLite:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --save
```

As dependencias externas ficam registradas no `requirements.txt` e no `pyproject.toml`.

## Resultado esperado

Uma base SQLite integrada de cotacoes que permita comparar precos entre estados e consultar historico de produtos hortifrutigranjeiros comercializados no atacado.
