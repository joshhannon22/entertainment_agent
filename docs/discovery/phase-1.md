Just a simple Agent to learn how it works. The user is me who will be playing around with its simple use cases of using tools for a query. Its successful if it can take in a message and use tools correctly to answer the message. It should run agains evals and see that the answer it provides is a sufficient one to the user. Some constraints are this should be as free as possible. Its not using any cloud resources yet and doesnt need to run in containers. It should just be able to run locally well. Things out of scope for now include cloud resources, kubernetes, containers. This should all run locally for the time being.

1. Problem: what pain does the user have today? 
    User wants a show to watch or some information about them. User wants deals on games. User wants info on books.
2. User and example request: who is asking, and what's one typical question?
    Find a show based on an input
    Find game deals
    find details on books
3. Success metric: how would you know Phase 1 works? Give at least one metric for behavior (did it pick the right tools?) and one for cost or latency.
    It picks tools to run to find information about the users query. It picks correct tools to use for the queries. 
    It should pass 80% of 10 eval questions
    It should have a ceiling of $0.10 per request
    Target Latency isnt as important right now as we are learning in this Phase
4. Constraints: free tiers, unreliable third-party APIs, and how much it can cost per request.
    Cheap models, free tiers if possible. Thrid party APIs that are free to use. Limit cost per request as much possible. This is all inital Agent testing. Specifically for 
5. Out of scope: what are you not doing in Phase 1? (Look at the plan. Plenty is deferred to later phases.)
    Keeping everything local for now. No cloud, kubernetes, or other integrations yet. just a local first Agent.
    Semantic Search - tools should help answer straight forward questions for now. Not make decisions based on user input feelings.