# Decisoes tecnicas

Este documento registra decisoes tomadas durante o desenvolvimento do scraper.

O objetivo e manter o historico de escolhas importantes sem depender apenas de conversas ou commits.

## 2026-06-01

### Comecar pela documentacao

Decisao: antes de implementar o scraper, criar uma base documental com objetivo, fontes, modelo de dados e plano de implementacao.

Motivo: o projeto envolve varias fontes com formatos diferentes. Documentar primeiro reduz retrabalho e ajuda a manter o escopo claro.

### Priorizar fontes HTML ou API publica

Decisao: a primeira versao deve priorizar fontes em HTML ou API publica.

Motivo: fontes em PDF, planilhas baixadas manualmente ou paginas com bloqueios tendem a exigir tratamento especifico. Elas devem ficar para depois que o fluxo principal estiver funcionando.

### Usar Python como base do scraper

Decisao: usar Python para a implementacao inicial.

Motivo: Python tem bibliotecas maduras para requisicoes HTTP, parsing de HTML, tratamento tabular e persistencia simples.

### Usar SQLite como persistencia inicial

Decisao: salvar os dados em SQLite desde a primeira versao.

Motivo: o projeto precisa de um arquivo final consultavel e simples de manter. SQLite atende esse objetivo sem exigir servidor de banco.

### Separar coletores por fonte

Decisao: cada CEASA deve ter seu proprio coletor.

Motivo: as fontes possuem estruturas diferentes. Separar os coletores evita acoplamento e facilita manutencao quando uma fonte muda.

### Comecar pela CEASA-PE

Decisao: iniciar pela coleta de HTML bruto da CEASA-PE.

Motivo: a fonte possui paginas de cotacao separadas por categoria e tabelas proximas do modelo de dados desejado. Isso permite implementar uma primeira coleta simples antes do parser e da gravacao em SQLite.

### Usar `.env` para valores locais

Decisao: manter um `.env` local para os valores padrao da CLI e um `.env.example` versionavel.

Motivo: a coleta precisa de configuracoes locais, como fonte, diretorio de HTML bruto e timeout HTTP. O `.env.example` documenta esses valores sem versionar configuracoes locais.

### Limitar ritmo de requisicoes

Decisao: configurar um intervalo minimo entre requisicoes HTTP.

Motivo: a coleta deve ser conservadora para reduzir risco de bloqueio e evitar carga desnecessaria nas fontes publicas.

### Separar configuracao de fontes

Decisao: mover a URL base da CEASA-PE para `config/fontes.json`.

Motivo: fontes sao configuracoes do scraper, nao configuracoes locais de ambiente. Manter isso em arquivo proprio evita misturar lista de fontes com caminhos locais, timeouts e banco.

### Descobrir categorias da CEASA-PE em tempo de execucao

Decisao: remover a lista fixa de categorias da CEASA-PE e descobrir os links de categoria a partir da pagina base.

Motivo: se a fonte adicionar, remover ou renomear categorias, o scraper deve acompanhar a estrutura disponivel sem exigir mudanca de codigo para o caso comum.

### Gravar cotações normalizadas em SQLite

Decisao: adicionar `SQLiteStorage` e o modo `--save` na CLI.

Motivo: o projeto precisa manter o HTML bruto para auditoria, mas o resultado util deve ficar em um banco local consultavel. A tabela `cotacoes` usa uma chave hash para evitar duplicar registros iguais em execucoes repetidas.

### Evoluir para schema relacional

Decisao: substituir a tabela unica por tabelas normalizadas: `estados`, `ceasas`, `categorias`, `produtos`, `unidades` e `cotacoes`.

Motivo: o processamento da CEASA-PE foi validado de ponta a ponta. Com isso, o banco pode seguir mais proximo do modelo planejado sem antecipar regras complexas de normalizacao. Se existir uma tabela unica antiga, a gravacao e interrompida para que o banco local seja recriado manualmente.

Atualizacao: backup automatico foi removido. Se existir banco antigo com tabela flat, a gravacao e interrompida para que o arquivo local seja excluido manualmente.

### Coletar datas de cotacao anteriores

Decisao: adicionar `COTACOES_TARGET_DATE`, `COTACOES_QUOTES_BACK`, `--target-date` e `--quotes-back`.

Motivo: a CEASA-PE permite consultar cotacoes antigas pelo parametro `data`. A configuracao deve representar datas de cotacao disponiveis, nao dias corridos. Exemplo: `--quotes-back 30` coleta a data alvo e mais 30 datas anteriores que realmente tenham cotacao.

Detalhe: quando `COTACOES_TARGET_DATE` fica vazio, a data alvo e a data atual do sistema. Informar `COTACOES_TARGET_DATE=01/02/2026` nao significa coletar de fevereiro ate hoje; significa usar `01/02/2026` como ponto de partida.

### Continuar em falha parcial por categoria

Decisao: ao processar multiplas categorias, uma categoria sem tabela nao derruba a execucao inteira.

Motivo: em 29/05/2026, `flores` e `organicos` nao retornaram tabela, mas as demais categorias tinham cotacoes validas. O scraper deve registrar o erro e salvar o que foi coletado com sucesso.

### Manter somente o raw mais recente na pasta principal

Decisao: ao salvar HTML bruto, manter em `data/raw/<fonte>/` somente o arquivo mais recente por fonte, categoria, data de cotacao consultada e dia de execucao. Arquivos anteriores do mesmo grupo sao movidos para `data/raw/<fonte>/old/`.

Motivo: a pasta principal deve ficar facil de inspecionar durante o desenvolvimento, sem perder totalmente os arquivos antigos gerados no mesmo dia.

### Compactar raws antigos sob demanda

Decisao: disponibilizar um comando para compactar os `.html` soltos de `data/raw/<fonte>/old/` em um novo `.zip` dentro da propria pasta `old`, removendo os `.html` originais depois da compactacao.

Motivo: a pasta `old` tende a crescer com o tempo. A compactacao sob demanda preserva o historico bruto sem deixar muitos arquivos HTML soltos acumulados no diretorio.

### Reutilizar raw ativo antes de requisitar

Decisao: permitir que `COTACOES_REUSE_RAW_BEFORE_REQUEST=true` faca a coleta reutilizar o HTML correspondente em `data/raw/<fonte>/` antes de abrir uma nova requisicao HTTP.

Motivo: a opcao reduz requests repetidas durante desenvolvimento e reprocessamento. A busca fica limitada a pasta raw principal para evitar misturar historico antigo ou arquivos compactados no fluxo normal de coleta.

### Implementar CEASA-MG pela ultima cotacao

Decisao: implementar a CEASA-MG a partir da tabela de preco mais comum da ultima cotacao. Cada coluna de cidade vira uma cotacao separada, registrando a cidade em `procedencia`.

Motivo: a fonte nao expoe categorias nem acesso confiavel a cotacoes anteriores. Como a tabela publica traz uma cidade por coluna, usar `procedencia` preserva a cidade sem alterar o schema relacional neste momento.

### Normalizar unidades sem perder o valor original

Decisao: a normalizacao de unidades deve preservar a unidade original da fonte e gerar campos derivados para analise.

Motivo: a CEASA-PE mistura unidade, embalagem e quantidade no mesmo texto. Exemplos: `Kg`, `Cx.20Kg`, `Cx.30 Dz`, `Molho 0,350 Kg`, `Cx12Unid.1L`. Se o scraper apenas sobrescrever esse texto por uma sigla simples, parte da informacao comercial sera perdida.

Caminho recomendado: criar um normalizador especifico de unidade que extraia `embalagem`, `quantidade_minima`, `quantidade_maxima`, `unidade_medida` e `detalhe`, mantendo tambem `unidade_original`.

## 2026-06-02

### Implementar CEASA-PR por PDFs diarios

Decisao: implementar a CEASA-PR pela pagina anual unificada, onde cada cidade, mes e dia aponta para um PDF diario.

Motivo: a estrutura a partir de 2022 concentra as cidades em paginas anuais. Como os nomes dos arquivos PDF variam por cidade, o scraper deve localizar o link no HTML em vez de montar a URL por padrao de nome.

Detalhe: para esta fonte, as categorias do projeto representam as cidades descobertas na pagina anual. O parser usa `pypdf` para extrair o texto dos PDFs e mapear produto, tipo, unidade, situacao de mercado, precos e procedencia para o modelo normalizado.

### Implementar CEASA Campinas por links de PDF

Decisao: implementar a CEASA Campinas a partir da pagina de cotacoes anteriores, onde cada data aponta para um PDF.

Motivo: a fonte publica as cotacoes em uma lista paginada de datas. Como os links estao no HTML e podem mudar ao longo do tempo, o scraper deve descobrir os PDFs pela propria pagina em vez de montar URLs manualmente.

Detalhe: o coletor busca o PDF mais recente ate a data alvo. O parser usa as secoes encontradas dentro do PDF como categorias dos registros, sem manter lista fixa de grupos de produtos.

### Avaliar PROHORT antes de novos scrapers individuais

Decisao: antes de implementar novas fontes individuais, comparar a fonte local com o `ProhortDiario.txt` da CONAB usando criterios de longevidade, detalhes uteis e velocidade de atualizacao.

Motivo: o PROHORT cobre muitas CEASAs em formato padronizado desde 2022, mas nao substitui automaticamente as fontes individuais. Algumas fontes locais podem ter historico mais antigo, campos mais detalhados ou atualizacao mais rapida.

Detalhe: quando PROHORT e fonte individual cobrirem a mesma CEASA/data, as fontes podem ser combinadas. O PROHORT pode servir como base nacional padronizada, enquanto a fonte individual complementa com detalhes como minimo, maximo, classificacao, procedencia ou situacao de mercado.

## 2026-06-03

### Complementar dados com PROHORT em comando separado

Decisao: manter os scrapers individuais como fonte principal e adicionar um comando separado para complementar registros ja salvos com o PROHORT.

Motivo: o fluxo diario nao deve gerar dois resultados diferentes para a mesma cotacao. A fonte individual continua mandando no registro; o PROHORT entra depois apenas para preencher campos vazios quando houver correspondencia confiavel.

Detalhe: o complemento nao sobrescreve valores preenchidos. O comando compara CEASA, data, produto e unidade, e registra `fonte_complemento`, `url_complemento` e `data_complemento` quando algum campo e preenchido. Quando o PROHORT tem um produto do mesmo dia e da mesma CEASA que ainda nao existe na fonte principal, ele insere uma cotacao complementar sem minimo, maximo, classificacao ou situacao de mercado. Quando a categoria da fonte principal nao for conhecida, usa a categoria `prohort-complemento`.

### Implementar CEASA-CE antes de novas fontes pendentes

Decisao: escolher a CEASA-CE como proxima fonte individual.

Motivo: a pagina oficial de boletins lista links diretos para PDFs por entreposto e categoria, sem formulario por produto. Os PDFs trazem data, unidade, preco minimo, comum, maximo, procedencia e situacao de mercado, oferecendo mais detalhe que o PROHORT para uso como fonte principal.

Detalhe: a primeira versao coleta boletins atuais descobertos em `boletim.php`. Nao foi implementado historico por data para esta fonte.
