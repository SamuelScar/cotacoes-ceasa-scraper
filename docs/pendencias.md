# Pendencias

## Resumo rapido

| Pendencia | Resumo |
| --- | --- |
| [Progresso da execucao](#progresso-da-execucao) | Exibir percentual, fase atual, itens concluidos, tempo decorrido e estimativa confiavel nos comandos longos. |
| [Coletas historicas parciais](#coletas-historicas-parciais) | Preservar e persistir raws validos mesmo quando uma fonte conclui apenas parte do historico solicitado. |
| [Dados compactados e Git LFS](#dados-compactados-e-git-lfs) | Versionar os dados como arquivo compactado com Git LFS e controlar descompactacao, validacao e substituicao segura. |
| [Paralelismo entre fontes](#paralelismo-entre-fontes) | Executar downloads de fontes independentes em paralelo, mantendo requisicoes internas e persistencia SQLite sequenciais. |
| [Execucao continua como crawler](#execucao-continua-como-crawler) | Avaliar e implementar execucoes periodicas para coletar e persistir somente dados novos. |

## Progresso da execucao

Exibir o progresso dos comandos longos, principalmente `tudo`, para permitir
acompanhar quanto da execucao ja foi concluido.

A saida deve informar:

- percentual geral concluido;
- fase atual, como download ou persistencia;
- fonte e categoria atuais;
- quantidade concluida e total conhecido;
- tempo decorrido e estimativa de tempo restante, quando confiavel.

O calculo deve considerar que algumas fontes descobrem categorias e datas
durante a execucao. Nesses casos, o total e o percentual podem ser atualizados
conforme novos itens forem descobertos, sem apresentar uma estimativa enganosa.

## Coletas historicas parciais

Quando uma fonte encontra apenas parte da quantidade de datas solicitada, os
raws validos ja baixados devem ser preservados e persistidos. A fonte nao deve
ser tratada como falha total somente porque nao atingiu o tamanho completo da
janela configurada.

A implementacao deve:

- diferenciar fonte concluida, parcialmente concluida, com falha e ignorada;
- persistir os raws validos baixados antes de uma falha ou esgotamento parcial;
- manter a falha de conexao ou bloqueio registrada sem descartar resultados
  anteriores validos;
- informar por fonte quantos raws foram baixados, persistidos e ficaram
  aguardando persistencia;
- separar os totais consolidados por fase, sem somar novamente a mesma fonte no
  download e na persistencia;
- aplicar retry para respostas incompletas, como `IncompleteRead`, respeitando o
  backoff e o limite de tentativas existentes;
- documentar que janelas incrementais menores, como
  `COTACOES_QUOTES_BACK=99`, reduzem o risco de perder uma fase inteira;
- manter `COTACOES_QUOTES_BACK=infinito` como opcao para buscar ate o fim do
  historico disponivel sem exigir uma quantidade fixa de datas.

O fluxo `tudo` deve continuar processando automaticamente os raws validos de
uma fonte parcialmente concluida. O comando `salvar` permanece como recuperacao
para raws ativos que nao foram persistidos durante uma execucao interrompida ou
com falha.

## Dados compactados e Git LFS

A pasta `data/` deve manter a estrutura atual durante a execucao, mas ser
versionada como um unico arquivo compactado para evitar milhares de raws no
repositorio. Como esse arquivo crescera continuamente e cada alteracao de um
arquivo binario pode aumentar muito o historico normal do Git, ele deve ser
armazenado com Git LFS.

Fluxo proposto:

1. Manter somente `data.tar.gz` versionado com Git LFS.
2. Antes de executar um comando, descompactar o arquivo preservando
   `data/cotacoes.sqlite`, `data/raw/` e suas subpastas.
3. Executar o scraper normalmente, adicionando raws e registros ao banco
   existente.
4. Ao encerrar, inclusive em falhas ou interrupcoes, gerar um novo arquivo
   compactado temporario.
5. Validar o arquivo temporario e substituir `data.tar.gz` de forma atomica.
6. Remover a pasta `data/` descompactada somente depois da substituicao.

A implementacao deve fornecer um script unico para envolver os comandos
existentes, por exemplo:

```bash
./scripts/cotacoes tudo
./scripts/cotacoes baixar
./scripts/cotacoes salvar
./scripts/cotacoes app --source ceasa-pe --save
```

Cuidados necessarios:

- usar um lock para impedir duas execucoes alterando o mesmo pacote;
- nunca sobrescrever diretamente o arquivo compactado valido;
- manter `data/` e arquivos compactados temporarios no `.gitignore`;
- configurar `data.tar.gz` no `.gitattributes` para uso do Git LFS;
- documentar a instalacao do Git LFS para quem clonar o repositorio;
- acompanhar armazenamento e transferencia consumidos no provedor;
- considerar armazenamento externo quando o volume deixar de ser adequado
  ate mesmo para Git LFS.

O Git LFS evita que o repositorio Git comum carregue os dados completos, mas
continua armazenando cada versao enviada de `data.tar.gz`. Portanto, ele reduz
o peso do clone normal, mas nao elimina o crescimento do armazenamento remoto.

## Paralelismo entre fontes

A coleta de todas as fontes atualmente ocorre de forma sequencial. Executar
fontes independentes em paralelo pode reduzir bastante o tempo total, pois boa
parte da execucao fica aguardando respostas HTTP.

O paralelismo deve ocorrer somente entre fontes. As requisicoes internas de
cada fonte devem continuar sequenciais para preservar cookies, navegacao,
ordem de descoberta e o intervalo configurado entre requisicoes.

Fluxo proposto:

1. Criar uma fila com as fontes configuradas.
2. Executar uma quantidade limitada de fontes simultaneamente.
3. Manter um coletor e uma sessao HTTP isolados por fonte.
4. Salvar cada raw imediatamente, como ocorre atualmente.
5. Agrupar as falhas por fonte sem interromper as demais.
6. Processar e persistir os raws depois do download paralelo.

Exemplo de comando futuro:

```bash
docker compose run --rm baixar --workers 3
docker compose run --rm tudo --workers 3
```

Cuidados necessarios:

- iniciar com poucos workers e tornar o limite configuravel;
- garantir que a saida identifique a fonte em todas as mensagens;
- preservar delay, retry, `Retry-After` e interrupcao por bloqueio em cada
  fonte;
- evitar que duas tarefas escrevam o mesmo raw;
- nao compartilhar coletores ou sessoes HTTP entre threads;
- evitar escritas concorrentes no SQLite, preferindo uma fase unica de
  persistencia depois dos downloads;
- permitir cancelar a execucao e encerrar os workers corretamente;
- medir o impacto antes de aumentar o paralelismo.

O objetivo nao e aumentar agressivamente o numero de requisicoes para uma
mesma CEASA. O ganho deve vir da espera simultanea por fontes diferentes.

## Execucao continua como crawler

Transformar o scraper em um crawler significa manter um processo responsavel
por verificar periodicamente todas as fontes, coletar somente dados novos,
persistir os resultados e continuar aguardando a proxima execucao.

O crawler deve reutilizar os fluxos existentes em vez de implementar outra
logica de coleta. Ele seria apenas responsavel por agendar e coordenar chamadas
equivalentes ao comando `tudo`.

Fluxo proposto:

1. Iniciar o servico e validar configuracao, diretorios e banco.
2. Executar uma rodada de download para as fontes agendadas.
3. Processar os raws baixados e salvar somente registros novos.
4. Registrar o resultado da rodada por fonte.
5. Aguardar o proximo horario configurado.
6. Encerrar de forma controlada ao receber um sinal do container.

Exemplo de configuracao futura:

```env
COTACOES_CRAWLER_INTERVAL_MINUTES=60
COTACOES_CRAWLER_WORKERS=3
```

Exemplo de servico futuro:

```bash
docker compose up crawler
```

Cuidados necessarios:

- impedir que uma nova rodada comece antes da anterior terminar;
- permitir frequencias diferentes para fontes com atualizacoes distintas;
- manter a coleta idempotente para nao duplicar registros;
- persistir o estado minimo necessario para retomar apos reinicio;
- aplicar backoff maior quando uma fonte permanecer indisponivel;
- registrar inicio, fim, duracao, arquivos baixados, registros novos e falhas
  de cada rodada;
- disponibilizar uma verificacao de saude para identificar processo travado;
- encerrar corretamente durante compactacao, download ou persistencia;
- separar erros temporarios de falhas que exigem manutencao do coletor.

Antes de manter um processo Python permanentemente ativo, deve ser avaliada
uma alternativa mais simples: executar `docker compose run --rm tudo` por
agendamento externo, como cron ou systemd timer. Um servico crawler dedicado
passa a ser interessante quando forem necessarios intervalos por fonte,
retentativas coordenadas, estado persistente e observabilidade continua.

## Desempenho do processamento de raws

Melhorar o desempenho do processamento de raws, principalmente nas fontes com
grande volume de arquivos e nos parsers de PDF.

Foi realizado um estudo do relatorio
`data/relatorios/persistencia_20260610_204715_856649.md` para identificar o
gargalo da persistencia completa iniciada em `2026-06-10T20:47:15+00:00`.

Resultados encontrados:

- duracao total de `1h53min54s`;
- processamento dos raws: `1h30min34s`, aproximadamente `79,5%` do total;
- persistencia no SQLite: `19min07s`, aproximadamente `16,8%` do total;
- complemento PROHORT: `3min35s`, aproximadamente `3,1%` do total;
- CEASA-PR, CEASA-PE e CEASA-ES concentraram aproximadamente `85%` da
  execucao;
- a CEASA-PR consumiu aproximadamente `50min12s`, principalmente durante o
  processamento de `618` PDFs.

O estudo confirmou que o principal gargalo esta no processamento dos raws. O
parser, principalmente a extracao e interpretacao dos PDFs, provavelmente
representa uma parte relevante desse custo, mas o fluxo deve ser medido por
etapa antes das otimizacoes.

Medicoes necessarias:

- listagem e leitura dos arquivos;
- extracao de texto dos PDFs;
- parser especifico de cada fonte;
- normalizacao e criacao das cotacoes;
- persistencia no SQLite.

Melhorias a avaliar:

- ignorar raws que ja foram processados e persistidos sem alteracao;
- manter uma opcao explicita para reprocessamento completo;
- armazenar ou reutilizar texto extraido de PDFs quando o raw nao mudar;
- processar raws independentes em paralelo com limite configuravel;
- reduzir trabalho repetido dentro dos parsers e normalizadores;
- priorizar a investigacao dos PDFs da CEASA-PR;
- registrar duracao por fonte, arquivo e etapa nos relatorios.

Qualquer otimizacao deve preservar os resultados atuais dos parsers, a
proveniencia dos registros e a capacidade de reconstruir o banco a partir dos
raws.

## Aprimorar sincronizacao incremental com Supabase

Os modos incremental e completo, com envio em lotes e retomada, ja estao
disponiveis. Melhorias futuras:

- detectar e atualizar alteracoes em coletas e cotacoes antigas usando chaves
  unicas de negocio;
- detalhar nos relatorios quantos registros foram inseridos, atualizados ou
  ignorados por tabela.
