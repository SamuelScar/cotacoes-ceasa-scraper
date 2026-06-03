# Comandos

Comandos principais para preparar o ambiente e executar o scraper.

## Preparar ambiente

Criar `.env` local:

```bash
cp .env.example .env
```

Buildar a imagem:

```bash
docker compose build
```

Baixar os arquivos brutos de todas as categorias:

```bash
docker compose run --rm baixar
```

Processar os arquivos brutos baixados e salvar no SQLite:

```bash
docker compose run --rm banco
```

Baixar, processar e salvar no SQLite em um unico comando:

```bash
docker compose run --rm tudo
```

Compactar HTMLs antigos da pasta `old`:

```bash
docker compose run --rm compactar-old
```

Esse comando compacta os `.html` soltos de `data/raw/<fonte>/old/` em um novo `.zip` dentro da propria pasta `old` e remove os `.html` originais depois que o arquivo compactado e criado. Arquivos `.zip` anteriores permanecem na pasta.

Complementar cotacoes salvas com dados do PROHORT:

```bash
docker compose run --rm complementar-prohort
```

Esse comando deve ser executado depois dos scrapers individuais. Ele le o banco SQLite, baixa o `ProhortDiario.txt` e preenche `preco_comum` vazio quando encontra correspondencia confiavel por CEASA, data, produto e unidade. Dados ja preenchidos nao sao sobrescritos. Se o PROHORT tiver um produto do mesmo dia e da mesma CEASA que a fonte principal nao trouxe, o comando insere uma cotacao complementar marcada com `fonte_complemento = prohort`. Quando a categoria da fonte principal nao for conhecida, a cotacao entra na categoria `prohort-complemento`.

## Comandos avancados

Tambem e possivel passar argumentos diretamente para a CLI pelo servico `app`.

## Flags da CLI

- `--source`: escolhe qual fonte sera coletada, como `ceasa-pe`, `ceasa-mg` ou `ceasa-pr`.
- `--list-categories`: lista as categorias disponiveis da fonte sem baixar nem salvar cotacoes.
- `--parse`: baixa os dados brutos e extrai as cotacoes, mas nao salva no banco.
- `--save`: baixa os dados, extrai as cotacoes e salva no SQLite.
- `--target-date`: define uma data limite de cotacao; se nao for usada, o sistema busca a ultima cotacao disponivel.
- `--quotes-back`: coleta tambem datas anteriores a data limite, quando a fonte suporta historico.
- `--process-raw`: processa arquivos brutos ja salvos em `data/raw`, sem acessar a internet.
- `--complement-prohort`: complementa cotacoes ja salvas usando o PROHORT, sem sobrescrever campos preenchidos.
- `--raw-dir`: define outra pasta para ler ou salvar arquivos brutos.
- `--database-path`: define outro arquivo SQLite para salvar os registros.
- `--base-url`: sobrescreve temporariamente a URL base da fonte configurada.
- `--prohort-url`: sobrescreve temporariamente a URL do arquivo `ProhortDiario.txt`.
- `--http-timeout-seconds`: define o tempo maximo de espera para cada requisicao HTTP.
- `--request-delay-seconds`: define o intervalo minimo entre uma requisicao e outra.
- `--archive-raw-old`: compacta arquivos brutos antigos da pasta `old`.

## Alterar fonte

Os comandos principais usam a fonte configurada em `COTACOES_SOURCE`.

```env
COTACOES_SOURCE=ceasa-mg
```

Tambem e possivel passar a fonte direto no comando:

```bash
docker compose run --rm app --source ceasa-mg --parse
```

Exemplo para CEASA-PR em uma data especifica:

```bash
docker compose run --rm app --source ceasa-pr --target-date 02/06/2026 --save
```

Na CEASA-PR, as categorias representam as cidades descobertas na pagina anual da fonte.

Exemplo para CEASA Campinas:

```bash
docker compose run --rm app --source ceasa-campinas --save
```

Na CEASA Campinas, o scraper descobre os PDFs pela lista de datas da pagina de cotacoes anteriores.
O `--list-categories` mostra a pagina de cotacoes da fonte; os grupos de produtos sao descobertos dentro do PDF durante `--parse` ou `--save`.

Exemplo para CEASA-GO:

```bash
docker compose run --rm app --source ceasa-go --save
```

Na CEASA-GO, o scraper acessa a pagina anual, encontra a pagina mensal e baixa o PDF diario mais recente ate a data limite. A fonte suporta `--target-date` e `--quotes-back`; os grupos de produtos sao descobertos dentro do PDF.

Exemplo para CEASA-CE:

```bash
docker compose run --rm app --source ceasa-ce --save
```

Na CEASA-CE, as categorias representam boletins atuais por entreposto e tipo de produto descobertos na pagina oficial. A fonte nao suporta `--quotes-back`.

## Verificar CLI

Mostra os parametros disponiveis sem acessar a internet:

```bash
docker compose run --rm app --help
```

Teste controlado sem salvar no banco:

```bash
docker compose run --rm app --parse
```

## Listar categorias da CEASA-PE

Faz uma requisicao para a URL base e lista as categorias encontradas:

```bash
docker compose run --rm app --list-categories
```

## Baixar raw bruto

Baixar todas as categorias descobertas:

```bash
docker compose run --rm app
```

Os arquivos sao salvos em `data/raw/<fonte>/`. Para cada fonte, categoria, data de cotacao consultada e dia de execucao, a pasta principal fica somente com o raw mais recente. Arquivos anteriores do mesmo grupo no mesmo dia sao movidos para `data/raw/<fonte>/old/`.

## Baixar e extrair cotacoes

Extrair todas as categorias descobertas:

```bash
docker compose run --rm app --parse
```

Resultado observado em 2026-06-01:

```text
total: 326 cotacoes extraidas.
```

## Salvar no SQLite

Baixar, extrair e salvar todas as categorias descobertas:

```bash
docker compose run --rm app --save
```

Baixar, extrair e salvar uma data especifica:

```bash
docker compose run --rm app --target-date 29/05/2026 --save
```

Resultado observado para 29/05/2026:

```text
datas: 2026-05-29
aves-e-ovos: 9 cotacoes
carnes-e-laticinios: 15 cotacoes
cereais-e-diversos: 16 cotacoes
flores 2026-05-29: erro - Tabela de cotacoes da CEASA-PE nao encontrada.
flores: 0 cotacoes
frutas: 83 cotacoes
hortalicas: 81 cotacoes
organicos 2026-05-29: erro - Tabela de cotacoes da CEASA-PE nao encontrada.
organicos: 0 cotacoes
pescados: 12 cotacoes
total: 216 cotacoes extraidas. 216 registros novos salvos em data/cotacoes.sqlite.
```

Baixar, extrair e salvar datas de cotacao anteriores:

```bash
docker compose run --rm app --quotes-back 30 --save
```

Com `--quotes-back 30`, o scraper coleta a data limite e mais 30 datas anteriores que realmente tenham cotacao. Dias sem cotacao sao ignorados.

Se `--target-date` nao for informado e `COTACOES_TARGET_DATE` estiver vazio, a coleta busca a ultima cotacao disponivel na fonte.

Exemplo buscando a ultima cotacao disponivel como ponto de partida:

```bash
docker compose run --rm app --quotes-back 30 --save
```

Exemplo usando uma data especifica como ponto de partida:

```bash
docker compose run --rm app --target-date 29/05/2026 --quotes-back 30 --save
```

Nesse caso, o scraper coleta `29/05/2026` e mais 30 datas de cotacao anteriores encontradas. Ele nao coleta de `29/05/2026` ate hoje.

Quando uma categoria nao tem tabela para uma data, o erro e exibido e as outras categorias continuam sendo processadas.

Resultado observado em 2026-06-01:

```text
total: 326 cotacoes extraidas. 317 registros novos salvos em data/cotacoes.sqlite.
```

Os 317 registros novos ocorreram porque 9 registros ja tinham sido salvos em um teste anterior.

O caminho padrao do banco vem do `.env`:

```env
COTACOES_DATABASE_PATH=data/cotacoes.sqlite
```

Tambem pode ser alterado por comando:

```bash
docker compose run --rm app --save --database-path data/teste.sqlite
```

Sobrescrever URL base sem alterar `config/fontes.json`:

```bash
docker compose run --rm app --base-url https://www.ceasape.org.br/cotacao
```

Consultar quantidade de registros pelo Python:

```bash
docker compose run --rm --entrypoint python app -c "import sqlite3; con=sqlite3.connect('data/cotacoes.sqlite'); print(con.execute('select count(*) from cotacoes').fetchone()[0])"
```

Consultar alguns registros:

```bash
docker compose run --rm --entrypoint python app -c "import sqlite3; con=sqlite3.connect('data/cotacoes.sqlite'); rows=con.execute('select c.nome, ca.slug, p.nome_original, u.sigla, co.preco_comum, co.data_cotacao from cotacoes co join ceasas c on c.id = co.ceasa_id join categorias ca on ca.id = co.categoria_id join produtos p on p.id = co.produto_id left join unidades u on u.id = co.unidade_id limit 5').fetchall(); [print(row) for row in rows]"
```

Listar tabelas do banco:

```bash
docker compose run --rm --entrypoint python app -c "import sqlite3; con=sqlite3.connect('data/cotacoes.sqlite'); print([r[0] for r in con.execute(\"select name from sqlite_master where type='table' order by name\")])"
```

## Limites de coleta

O intervalo minimo entre requisicoes vem do `.env`:

```env
COTACOES_REQUEST_DELAY_SECONDS=2.0
```

Para evitar requisicoes repetidas quando o raw ja existe na pasta principal:

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```

Com essa opcao ativa, o scraper procura o raw correspondente em `data/raw/<fonte>/` antes de fazer download. A regra nao usa `old/` nem arquivos `.zip`.

A data limite e a janela de cotacoes tambem vem do `.env`:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=0
```

Com `COTACOES_TARGET_DATE=` vazio, a CLI busca a ultima cotacao disponivel. Com `COTACOES_QUOTES_BACK=0`, coleta somente essa cotacao.

Tambem pode ser alterado por comando:

```bash
docker compose run --rm app --request-delay-seconds 3
```
