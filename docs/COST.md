# Cost estimate

Nothing in this repository is deployed. This is the monthly price of the stacks as they are written, before anything is turned on. `python -m cost.estimate` prints the tables, and `tests/test_cost_estimate.py` checks the arithmetic. The quoted tables below are that output.

The list price is about **$1.03** a month. After the allowances the pricing pages document, and only if those allowances are still unused on the account, it is about **$0.87**. Rewriting the two S3 objects every minute is the largest line.

## What is included

`StatusProbe` is one function in each of us-west-2, us-east-1, and eu-west-1. Each has an EventBridge rule on a one-minute rate, 128 MB of memory, and a 20 second timeout. The architecture is unset, and the CDK default is x86. `StatusPage` adds a fourth function, `status-publisher`, on the same schedule and with the same memory and timeout. The roadmap item names the three probes. The publisher is included so the total covers the schedule that is actually in the template.

`status-quorum` has no schedule. The estimate gives it zero invocations.

`probe-results` is one on-demand global table. The stack is in us-east-1, with replicas in us-west-2 and eu-west-1. TTL is 7 days. Point-in-time recovery is on for every replica. The publisher writes `index.html` and `status.json` to a private bucket in us-east-1, and a CloudFront distribution in price class 100 reads that bucket. The distribution is pay-as-you-go. The template does not select a flat-rate plan. The `status-changes` topic has no subscription.

`PROBE_ENDPOINTS` defaults to one URL, `https://example.com`. A longer list multiplies the writes, the reads, and the JSON.

## Assumptions and the monthly total

Rows marked assumed are workload guesses. Nothing is deployed, so there is no measured duration and no traffic. The other rows come from the CDK stacks and the probe code.

```
Prices checked 2026-11-21.

| Input | Value |
| --- | --- |
| Probe count | 3 functions, one per region |
| Publisher | 1 function in us-east-1, same schedule |
| Schedule | every 1 minute, 30-day month |
| Invocations per scheduled function | 43,200 |
| Memory | 128 MB, x86 |
| Probe duration | 300 ms, assumed |
| Publisher duration | 400 ms, assumed |
| Quorum invocations | 0, it has no schedule |
| Endpoints | 1 |
| Observation item | 118 bytes, under 1 KB |
| Stored observation | 266 bytes, with overhead |
| Retention | 7 days |
| Page views | 1,000 a month, assumed |
| Viewer group | us, inside Price Class 100 |
| Page objects | index.html 2009 bytes, status.json 272 bytes |
| S3 writes | 2 puts a minute, the page and the JSON |
| S3 reads | 2 gets a page view, assumed cache misses |
| Status changes | 0 a month, assumed, no subscription |
| Regions | us-west-2, us-east-1, eu-west-1 |

| Line | List price | After allowances |
| --- | ---: | ---: |
| Lambda | $0.1516 | $0.0000 |
| DynamoDB writes us-west-2 | $0.1350 | $0.1350 |
| DynamoDB writes us-east-1 | $0.1350 | $0.1350 |
| DynamoDB writes eu-west-1 | $0.1523 | $0.1523 |
| DynamoDB reads us-east-1 | $0.0081 | $0.0081 |
| DynamoDB storage us-west-2 | $0.0034 | $0.0015 |
| DynamoDB storage us-east-1 | $0.0034 | $0.0015 |
| DynamoDB storage eu-west-1 | $0.0038 | $0.0016 |
| S3 | $0.4328 | $0.4328 |
| CloudFront | $0.0022 | $0.0000 |
| SNS | $0.0000 | $0.0000 |
| **Total** | **$1.0274** | **$0.8678** |

If every scheduled invocation ran for its 20 second timeout, the list price would be $8.1104 and the price after allowances would be $1.4012.
```

The month is 30 days, which is the month the Lambda pricing examples use. One rule then fires 43,200 times.

A probe's billed duration is assumed to be 300 ms, and the publisher's 400 ms. One HTTP attempt gives up after 5 seconds. A timeout or a connection failure is tried once more, so one endpoint can take 10.25 seconds. Both functions time out at 20 seconds. The last line of the quote is that ceiling: every scheduled invocation billed for the full 20 seconds. The quorum function stays at zero invocations.

A page view is one HTTPS request for `index.html` and one for `status.json`. The viewer is in the United States, Mexico, and Canada group, which price class 100 includes. The other group in that price class, Europe, Israel, and Türkiye, has a higher request price. Passing `viewer="europe"` uses it. The cache holds an object for at most 60 seconds, and the estimate treats every view as a miss, so each view is also two S3 GETs. Data transferred from S3 to CloudFront is free.

A steady month has no change from up to down or from down to up. The first up or down is stored and is not published. Later minutes with the same status do not write that item and do not publish. The publisher still reads it every minute.

## How the units are counted

Each probe writes one item per endpoint per minute to the replica beside it. The item is the shape `probe.writer.to_item` builds for a successful check of `https://example.com`: a 118 byte item, which is one write unit. [Item sizes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html) sizes strings as their UTF-8 bytes, numbers as about one byte per two significant digits plus one byte, and a Boolean or null as one byte, all plus the attribute name. For storage, that page adds 100 bytes per item, and another 48 bytes for a global table. Request units do not include that overhead. A stored observation is 266 bytes. Seven days of observations from three regions, plus one status item, is what each replica holds.

A write to a global table is a replicated write request unit in every replica, including the region that took the write. [Global table billing](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/global-tables-billing.html) says there is no cross-Region data transfer charge for that copy. This table has no global secondary index, so there is no separate single-region write unit. Ireland's replicated write price is higher than the two US regions, which is why the eu-west-1 line is larger.

[TTL](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html) says the delete in the region where the item expires does not consume a write unit, and the copy of that delete consumes a replicated write unit in each other replica. The estimate treats the expiry as starting in the region that wrote the item, so each region is billed for the deletes of the other two regions' items. TTL may lag a few days. A few extra days of this table are still inside the storage allowance, and the point-in-time recovery line would move by a fraction of a cent. That lag is not added on top.

The publisher queries the last three minutes once per endpoint. Nine items are under 4 KB, so an eventually consistent query is half a read unit. It then reads the stored status with a consistent read, which is one read unit. Both happen on the us-east-1 replica.

Point-in-time recovery is priced on the stored size of each replica. The pricing page's restore example is for adding a replica to a table that already holds data. This monthly number does not include that one-time restore. The template creates the three replicas with the table, and nothing is deployed.

Lambda's GB-second is the billed seconds times 128/1024. The request price and the first duration tier are the same in the three regions. S3 prices are the us-east-1 Standard rates. The bucket uses an S3-managed key, and the topic has no KMS key.

## Prices, checked 2026-11-21

| What | Price used | Page |
| --- | --- | --- |
| Lambda request, x86 | $0.20 per million | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) |
| Lambda duration, x86, first tier | $0.0000166667 per GB-second | same |
| DynamoDB on-demand read, us-east-1 and us-west-2 | $0.125 per million RRU | [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/) |
| DynamoDB on-demand replicated write, those regions | $0.625 per million rWRU | same |
| DynamoDB Standard storage, those regions | $0.25 per GB-month | same |
| Point-in-time recovery, those regions | $0.20 per GB-month | same |
| DynamoDB on-demand read, eu-west-1 | $0.1415 per million RRU | the region rate table on that page |
| DynamoDB on-demand replicated write, eu-west-1 | $0.705 per million rWRU | same rate table |
| DynamoDB Standard storage, eu-west-1 | $0.283 per GB-month | same rate table |
| Point-in-time recovery, eu-west-1 | $0.22 per GB-month | same rate table |
| S3 Standard PUT, us-east-1 | $0.005 per 1,000 | [S3 pricing](https://aws.amazon.com/s3/pricing/) |
| S3 Standard GET, us-east-1 | $0.0004 per 1,000 | same |
| S3 Standard storage, first 50 TB | $0.023 per GB-month | same |
| CloudFront HTTPS, United States, Mexico, and Canada | $0.0100 per 10,000, after the free requests | [CloudFront pay-as-you-go](https://aws.amazon.com/cloudfront/pricing/pay-as-you-go/) |
| CloudFront HTTPS, Europe, Israel, and Türkiye | $0.0120 per 10,000, after the free requests | same |
| CloudFront data transfer out, both of those groups | $0.085 per GB, after the first TB | same |
| SNS Standard API request, us-east-1 | $0.50 per million, after the first million | [SNS pricing](https://aws.amazon.com/sns/pricing/) |

The US East prose on the DynamoDB page states the $0.625, $0.125, $0.25, and $0.20 figures, and the global-table example uses the same $0.625 and $0.25 for US West (Oregon). The Ireland figures are the EU (Ireland) row of the rate table that page loads: [dynamodb.json](https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/dynamodb/USD/current/dynamodb.json). The Lambda examples state $0.0000166667 per GB-second and $0.20 per million requests, and [lambda.json](https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/lambda/USD/current/lambda.json) has those two numbers for US East (N. Virginia), US West (Oregon), and EU (Ireland). The S3 and SNS amounts are the Standard rows of [s3.json](https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/s3/USD/current/s3.json) and [sns.json](https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/sns/USD/current/sns.json). The CloudFront amounts are in the pay-as-you-go page itself. Price class 100 is the United States, Mexico, and Canada group and the Europe, Israel, and Türkiye group.

## Allowances, kept separate from the list price

The list-price column uses none of these. The other column subtracts only the following, and only when the rest of the account has not already used them. The up-to-$200 credit for new accounts is not applied. It is account-wide, it expires, and it is not a price for this stack.

* Lambda: 1 million requests and 400,000 GB-seconds a month, from the Lambda pricing page. Four functions at the assumed durations use 172,800 requests and 7,020 GB-seconds, so that column is zero. At the 20 second ceiling the duration passes 400,000 GB-seconds and the remainder is billed.
* DynamoDB: 25 GB of Standard storage a month in each Region. The on-demand examples on the pricing page subtract it, and this table is far under it, so storage goes to zero. Point-in-time recovery stays. The 25 WCU, 25 RCU, and 25 rWCU allowances are provisioned capacity. This table is on-demand, so the write and read lines stay at list price.
* CloudFront, always-free heading on the pay-as-you-go page: 1 TB of data transfer out and 10,000,000 HTTP or HTTPS requests a month. One example on that page charges HTTPS after 20,000,000 requests. This estimate uses 10,000,000. The 2,000 requests here are inside either figure, so that column is zero.
* SNS: the first 1 million requests a month are free. A steady month publishes nothing. A publish under 64 KB is one request. With no subscription there is no delivery charge. The rate table prices an HTTP delivery at $0.0000006 each, which would apply only after a subscription exists.

The S3 pricing page, in the text checked on 2026-11-21, describes the $200 credit for accounts created from July 15, 2025. It does not list a separate free amount of Standard storage or requests. S3 stays at list price.

## Left out

The four one-minute rules are `AWS::Events::Rule` on the default bus. EventBridge Scheduler is a different service. Its price on the [EventBridge pricing page](https://aws.amazon.com/eventbridge/pricing/) is 14 million free invocations a month, then $1.00 per million. The template does not create a Scheduler schedule, so that price is not used. The same page lists delivery to a service in the same account as free, and it does not publish a separate price for a schedule rule's own invocation. That invocation is left out of the total.

Each function has a log group kept for two weeks. What the functions write depends on errors, and this estimate does not price log ingestion or storage.

[Global table billing](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/global-tables-billing.html) says reads of the stream that global tables themselves make are not charged. Nothing else reads the stream.
